# Super-agent (experimental)

Super-agent turns incoming GitHub comments into durable Tickets. A restricted
Router Session decides whether a comment is work; a Worker Session does the
work in its own thread and directory; an Outbox posts the result back.

The feature is on by default (`super_agent` in `src/config/features.py`).
Turn it off in **Settings → Experiments → Super-agent** to stop new intake
and dispatch; running work still finishes. It is a new layer: normal Chat and
AI Dispatch are unchanged.

## App surface

The shell has one entry: `/inbox`. Primary navigation is **Inbox**, **Tasks**,
**Agents**, **Runs**, **Sources**, **Plugins**; Settings and auth stay available.
The old Chat, Projects, Memory and Automations surfaces are retired from the
shell and their deep links redirect to `/inbox`.

A manual request is a first-class Ticket: the Inbox composer calls
`POST /api/v1/autonomy/tasks`, which creates a synthetic manual Source,
Trigger/Thread, Ticket, Subscription and Worker launch without a GitHub round
trip. GitHub remains one Source among others.

The interaction is the task. Create it from the Inbox, track its activity on
the task page (`GET /api/v1/autonomy/tickets/{id}/activity`), and close it
there (`POST /api/v1/autonomy/tickets/{id}/close`). An instruction added before
a worker session exists stays on the task and is included when that session
starts. The worker stream is an execution log.

The CLI's `/jobs watch` and TUI can also follow that record live (the web task
page polls `.../activity` and does not read the stream):
`GET /api/v1/autonomy/tickets/{id}/activity/stream` is an SSE stream of the
activity rows, plus token-level output. Cloud/orchestration Worker runs are
tailed from the run's event log. Local daemon/native Workers post model text
and tool names to `POST /api/v1/autonomy/daemon/tickets/{id}/output` (daemon
token and live lease checked) while the vendor CLI runs, and the activity
stream tails that bounded per-Ticket log (`autonomy_daemon_output_*`, migration
140). Every frame except the server's synthetic source-change reset carries a
resumable id (activity event id, run id, run event sequence, daemon
`<epoch>.<seq>`, last output source) for `Last-Event-ID` reconnects. The CLI falls
back to polling `.../activity` when a deployment does not serve the stream, and
a daemon whose live channel is unavailable (old server without the endpoint or
migration, database error, or `CONDUCTOR_DAEMON_TOKEN_STREAM=0`) simply keeps
the report/activity path: streaming is best effort and never fails the Worker.
Only visible text and tool names are streamed; tool arguments, tool results and
prompts stay local. Live streaming exists for the Codex provider only; every
other native provider keeps the report/activity path. A local daemon uses the
Codex **app-server** transport by default (``CONDUCTOR_CODEX_APP_SERVER=0``
selects ``codex exec`` instead): app-server emits ``item/agentMessage/delta``
token deltas, so a local Codex turn reaches the TUI as it is produced. The exec
fallback emits completed items rather than token deltas; tool/progress items
still stream as they start and each completed message arrives whole (as one or
more whitespace-aligned events).

The daemon keeps a local circuit over the app-server transport, and only
*transport* failures count toward it: a subprocess that will not start, an
``initialize``/``thread/*``/``turn/start`` RPC that times out or errors, a
reader that dies unexpectedly, a protocol violation (an approval request in a
non-interactive run, or a frame that is not valid JSON or not a JSON object —
surfaced without the frame's content), and a completed turn that produced no
final answer. A failure the server itself reported for the model or task (an
``error`` event) and the user's cancellation say nothing about the transport
and never count. An app-server failure — or an empty answer — before any
visible event falls back to ``codex exec`` once, unless
``CONDUCTOR_CODEX_APP_SERVER_FALLBACK=0``; after something was published the
failure propagates (no duplicate text), and whether it counted still follows
the transport/turn split above. After
``CONDUCTOR_CODEX_APP_SERVER_FAILURE_LIMIT`` (3) consecutive transport
failures the circuit sends Codex to the exec path for
``CONDUCTOR_CODEX_APP_SERVER_COOLDOWN`` (300) seconds. Opening the circuit
logs one ``warning`` (failure count, cooldown seconds, re-enable time) and
closing it — the cooldown expiring, or a successful turn resetting the
counter — logs one ``info``. The daemon logger carries its own stderr handler
at INFO (dependency libraries keep their default levels, since their debug
output can carry run text), so both close lines are visible in a default
start. Neither log line ever contains prompt or output text.

### Output stream protocol (credential continuity)

Credential safety of a text stream is a property of the *concatenation*, so the
server keeps the continuity state in the same database transaction as the
events instead of beside them:

- **Session and epoch.** `autonomy_daemon_output_sessions` has one row per
  Ticket: the current `epoch`, `next_seq`, the daemon's `last_batch`, a
  `tainted` flag, and the bounded scan state (`guard_tail`, `guard_state`).
  `autonomy_daemon_output_events` is keyed by `(ticket_id, epoch, seq)` and
  references the session, so an event without its guard cannot exist. Every
  append locks the session row (`SELECT ... FOR UPDATE`) and writes the session
  update and the events in one transaction: workers serialize on the database,
  and a failed append produces no event and leaves the guard untouched. The
  lease fence runs in the same transaction: it locks the daemon claim row
  `FOR NO KEY UPDATE`, then share-locks the Ticket row (the report path's
  order). A revoke or a report (both write the claim) queues behind the appends
  already in flight and ahead of later ones, which re-read the changed row and
  are refused, so no append commits after a revoke and a continuous stream
  cannot starve one; a stop or reassignment writes the Ticket row and waits for
  the append in flight. The lock is deliberately not `FOR SHARE` (a new share
  locker joins the holders instead of queueing behind a waiting writer, so
  overlapping appends could keep a revoke waiting) and not `FOR KEY SHARE` (it
  never makes a revoke wait, so an append could commit after it). The
  first-in-first-out queue is a Postgres lock property, not an SQL guarantee;
  `tests/integration/test_daemon_output_postgres_locks.py` pins it on a real
  database. A Ticket DELETE (which cascades to the claim, session and events) locks
  the Ticket first and then needs the claim, the opposite of an append, so deleting
  a Ticket while an append is in flight can deadlock and PostgreSQL aborts one of
  the two. No production path deletes a Ticket; for an operator or a future purge,
  revoke the claim (or lock the claims) first so the order is claim, then Ticket.
- **The server allocates the epoch.** Epochs come from a database-wide sequence
  (`autonomy_daemon_output_epoch_seq`), never from the session row, so a session
  that is deleted and recreated cannot reissue an epoch a watcher already
  holds. A daemon's first batch for a run carries
  no `epoch` and `batch_seq` 1; the response returns the allocated `epoch`,
  `stored`, `head` (`<epoch>.<seq>`) and `tainted`. Every later batch echoes the
  epoch and increments `batch_seq`. A wrong epoch (or an epoch used under another lease) is a structured 409
  `stale_output_epoch`, a lease that is no longer live is a separate plain 409
  ("Lease is stale or Ticket is no longer running"), a replayed sequence is `duplicate_output_batch`, and a
  *skipped* sequence (a lost batch) taints the stream. The daemon never invents
  an epoch, and it keeps streaming only on the acknowledgements described next.
- **The daemon's sink.** Scanned events wait in a queue of 256 events or 128 KiB
  of text, which the flusher drains at 32 events per 0.2 s, one batch in flight
  at a time. The live channel ends for the run, text and tool names alike, on a
  local scan refusal, a discontinuity (below), a queue overflow, any HTTP or
  transport error (a 409 included), an acknowledgement whose epoch is not the
  one held, and any acknowledgement that is not exactly `stored: true` with
  `tainted: false`. That last rule fails closed: a missing, null or non-boolean
  `stored` or `tainted` is unknown, not clean (reason `bad_ack`; `stored: false`
  is `not_stored`). `tainted: true` is the server's own verdict, so text stops
  without a `stop` event. The Worker and its report are unaffected by any of
  these. The final flush before the report has one budget of 3 seconds in all
  (`_OUTPUT_STREAM_FINAL_FLUSH_S`; a request timeout bounds each socket
  operation, not the request, so a slow but alive server could otherwise hold
  the report back for 8 batches): what is left is dropped and the channel is
  disabled (`flush_timeout`). It is skipped when the lease is lost or the run is
  cancelled, so a stop acknowledgement never waits behind it. The sink advances its copy of the guard only past events it queued, so
  it always equals the state the server will hold: a tool name dropped on a full
  queue is a missing display row and does not stop text, while a dropped text
  event stops the stream for good (`buffer_overflow`, a `stop` event, watchers
  get `output_reset`). It yields to the event loop before each tool record, as it
  does between text chunks, so one stdout read made of hundreds of tool records
  cannot hold up cancellation, lease renewal or the flusher; the yield comes
  first so the stop check, the scan and the push stay one uninterrupted step.
  `add()` takes one event at a time (a lock that is never held across a
  request), so a second producer could not have its guard advance overwritten by
  a message that yields between its chunks.
- **One scanner, shared.** `src/server/utils/output_guard.py` is a pure function
  of `(state, event text)` used by both the daemon (so it never sends what the
  server would refuse) and the server (the authority). A text event is stored
  verbatim or not at all: it must end in whitespace (the stream never stops in
  the middle of a word) or be a `join` event, contain no terminal control, ESC/C1
  character, bidi embedding, override or isolate, Unicode tag character or lone
  surrogate (`control_character`) and no unbroken run longer than 512
  characters, and `window + text` (the whitespace-
  gap-compressed tail of already stored text, at most 2,560 characters) must contain
  no PEM marker, no credential-shaped token, `Bearer` scheme or `Basic` scheme followed by a base64-looking word, no credential key
  followed by `:`/`=`, no UUID after one of the sanitizer's id key words
  (`token`, `session`, `key`..) once its first two groups are out (`session 123e4567-e89b-`; the sanitizer waits for all five),
  no URL userinfo secret (next bullet), and must be unchanged by the shared log sanitizer. Other
  invisible characters (zero-width and joiner characters, variation selectors,
  fillers, the soft hyphen, the BOM, bidi marks) are stored as written but folded
  out, with an NFKC compatibility fold, before any rule reads the text, so
  `pass<ZWSP>word:` reads as the key it spells. The characters whose fold is a
  blank (the Unicode spaces, the spacing accents `´` `¨` `¯` `˘`.., two Arabic
  ligatures; a fixed list) are the exception: a URL parser reads them as
  ordinary userinfo characters, so they are kept as written and only the
  sanitizer is shown the folded form. Every
  credential rule except the URL one joins its parts (name, gap, value) with a gap of whitespace and
  `:`/`=` only, and the window keeps every gap bounded (a gap of up to 28
  characters is kept as is; a longer one is normalized, with blank runs reduced to
  one blank of the kind the run held (a plain blank ends a URL authority, a tab,
  a line break or a Unicode space does not) and runs of `:`/`=` capped at eight, and if still long reduced to its
  first 20 and last 8 characters), so an arbitrarily long gap (blanks, newlines or
  `: : :`) between a key and its value cannot push the key out of the window, and
  every threshold the sanitizer counts (a value of six, a token of eight) stays
  decidable. A randomized
  soundness test checks the premise against the real sanitizer: whatever the
  scanner accepts is unchanged by sanitizing the whole stream, in each
  projection, both as displayed and as the model wrote it. Tool names are rendered beside the text, so
  they pass through the same windows (a tool named `Bearer` followed by a token
  is refused like any other split credential). A watcher may also render a
  *projection* of the stream (print mode leaves reasoning out), and omitting an
  event creates adjacencies the full stream never had, so the scanner keeps one
  window per projection (everything; without reasoning; without tool names;
  text alone, persisted in `guard_state`) and an event must pass the window of
  every projection that shows it. Once the epoch is tainted nothing follows,
  tool names included (a name that outlived the text would form a projection no
  window tracks).
- **URL userinfo.** In `scheme://user:password@host` a non-empty password (other
  than a pure `*` mask or the exact `[REDACTED]`) refuses the event
  (`url_credential`), and so does a user name that is itself a token (24 or more
  of `[A-Za-z0-9_+-]` with a digit and a letter, `=` padding allowed; a tab, line
  break or Unicode space in it is ignored) whatever
  follows its colon: `TOKEN@host`, `TOKEN:@host` and `TOKEN:***@host`. The
  sanitizer alone catches such a URL only when the host looks like an e-mail
  domain, and after the password is out. The rule anchors on `://` after the fold (any scheme, extra or backward slashes, a tab or line break inside the anchor, full-width look-alikes)
  and reads the authority up to a blank or one of `/ ? #`; a backslash, a tab, a
  line break or a Unicode space stays inside it, and both the first and the last
  `@` are tried as the end of the userinfo, since parsers disagree. A raw `/ ? #`
  in the password cannot hide it: when the authority is `name:` followed by
  anything that is not a port (1-5 digits; an empty port counts as none) and an
  `@` follows before the next plain blank (a tab, a line break or a Unicode space
  is part of the password), the URL is refused. The password is
  withheld before the `@` arrives: a joined message that stops inside an authority is
  refused at `user:` plus a non-port character (`host:80` is not), also after
  an `@` that sits in the user name (`deploy@mail:hunter2pw`), a host in
  `[...]` counts as one only while it can still be an IPv6 literal, and an open
  authority longer than 512 characters ends the text, so `scheme://` and the
  whole userinfo stay inside the scan tail. It is the one rule that is a single
  run with no gap.
- **Message boundaries are not word boundaries.** A model message that ends
  mid-word gets a newline from the daemon so it ends on a display line break;
  that event is sent with `join: true`. The newline is synthetic, so for
  continuity the message runs straight into the next one: the open word is kept
  in the scan window and a token split over two messages (`eyJ` / `hbGci...`,
  `Bearer` / ` abc...`, `-----BEG` / `IN ...`) is judged as one token. Both
  readings are kept: the scanner holds windows for the text as displayed (newline
  included, so `word` / `Bearer` still shows `Bearer` as a word) and as the model
  wrote it (newline removed), and an event must pass both, in every projection. A joined
  chain cannot grow an unbounded word (a segment over 512 characters is refused).
- **Taint is one-way.** Anything that cannot be proven safe taints the epoch:
  that event and everything after it (text and tool names) are never stored, the
  scan window is dropped, and an `output_reset` marker is appended to the log.
  There is no pending buffer, nothing is dropped from the middle, and nothing
  "recovers".
- **Retained rows are consecutive.** Trimming removes only a prefix and expiry
  removes whole logs, and a read stops before any hole in the middle of a page,
  so the next read resets instead of joining the rows on either side.
- **Watchers are fenced by the same epoch.** The activity stream sends
  `{"kind":"output_reset","epoch":N,"reason":...}` before any text when a
  watcher's cursor names another epoch (a retry or new run), a trimmed gap, or
  an impossible position, and in-log when the stream was tainted. The reason is
  one of `new_epoch`, `continuity_lost`, `gap`, `cursor_invalid`; a reset
  carries no text. The line shell and print mode drop their sanitizer's
  half-parsed escape state, and the TUI (which cleans each block whole when it
  draws it) seals the current output block, on a reset; a sealed block prints its own visible
  boundary line, so the boundary survives `/compact` and block trimming and a
  prefix already shown is never joined to a suffix from another generation. Local-daemon output payloads carry the `epoch` (cloud
  run frames do not). The activity stream also resets a watcher whenever its
  live output changes source or generation (a cloud run to a daemon epoch and
  back, a new run id), including on a reconnect whose cursor names the other
  source; the clients treat any change of run or epoch (tool names included)
  without a reset as one too. The cursor of every frame, status frames included,
  records the source the watcher last received, so a reconnect after nothing but a
  status frame still resets across a source change. A cloud run source is named
  by the cursor's run part and is spelled `r<run id>` only when the cursor also
  carries a daemon position (or the source is not the Ticket's current run), so
  a reconnect into the same run after a daemon phase is not reset again; a
  cursor with a run part, a daemon part and no source (an older build, or a
  watcher that had received no output yet) is ambiguous and resumes with one
  conservative reset, which only seals the current block and loses nothing. The
  server's own synthetic reset carries no `id:` line, so a watcher cut between
  it and the text it announces re-reads that text and is reset again instead of
  skipping it. The cursor is echoed in the `id:` line of every frame, so a resume
  cursor is read part by part and a part that is not printable ASCII without a
  blank (a CR or LF would end the line and let the caller write fields into its
  own stream) is dropped: a daemon position becomes an impossible one, which the
  store answers with `cursor_invalid`, and the others count as absent.
- **Discontinuity.** `stream_process` reports every skipped or altered stdout
  record through `on_discontinuity(reason)`: `oversized_record`,
  `oversized_buffer` and `oversized_record_at_eof` (a record over 16 MiB is
  skipped), `invalid_utf8` (bytes were replaced while decoding) and `redacted`
  (the runner removed a credential value, which can be the very key word the
  guards look for). The Codex loop also treats a non-blank line that is not a
  JSON object, a record without a string `type` and a recognized `item.*`
  record with the wrong shape as corruption (`malformed_record`). On any of
  them the daemon stops text for the run (what was already queued is a complete
  clean prefix and is still sent) and posts a `stop` event, so the server
  taints the epoch and watchers receive an `output_reset` instead of an
  unexplained end. The callback is at-least-once and ordered: a reason whose
  callback raised is offered again before every later record and, when stdout
  ends, until it is delivered or its 3 tries are spent, then dropped with one
  ERROR log (a new occurrence of the same reason starts a fresh budget).
  `stream_process` deliberately never raises on it, because a live-output bug
  must not fail a healthy Worker; so the callback must not raise and must stop
  the consumer's own output before anything that can fail. Nothing is re-offered after a cancellation, a timeout or a closed
  generator, which the consumer sees itself. The daemon's callback only taints
  its local continuity guard and queues the `stop` event (no I/O); the flusher
  or the final flush posts it. If that post fails the live channel is disabled
  and nothing further is sent, so the server holds a clean prefix and watchers
  see an unexplained end instead of an `output_reset` (an accepted limit).
- **Only the daemon's Codex loop is guarded.** The six server-side native stream
  consumers (`stream_codex_turn`, `stream_grok_turn`, `stream_minimax_turn`,
  `stream_zcode_turn`, `stream_dsh_turn`, `stream_stepfun_turn`) feed the cloud
  run's stream (the activity stream's run source) and its persisted answer and,
  for Codex, DeepSeek-DSH and MiniMax, `native_agent` delegation results. That
  text is not scanned for credentials and carries no epoch (cloud run frames
  never did), so a skipped record cannot weaken a guard there. A caller that
  passes no callback gets a default one that logs a content-free WARNING (the
  reason and the CLI's basename) once per reason per run, instead of passing
  silently. `run_process` truncates stdout at 16 MiB without any signal, and
  non-JSON lines are ignored by the provider parsers and not reported.
- **Stopping the CLI.** Cancellation, timeout and generator close kill the CLI's
  process group (its id is the leader's pid, because the CLI starts in its own
  session), including a descendant that still holds stderr after the leader
  exited. A cancellation that lands while the CLI is still being started,
  however often it is repeated, is held until the start finishes; the pid is
  then recorded and the group killed before the first cancellation propagates,
  so a stop acknowledgement cannot read `gone()` as true for a child that
  already exists. The start is bounded (fork/exec plus pipe wiring), so
  absorbing the extra cancels cannot hang the caller. A run that ends on its
  own, even with a non-zero exit code, leaves the members of its group alone;
  only an abnormal exit kills. Limits that remain:
  (a) a descendant that left the group (`setsid`, `setpgid`, a job-control
  shell's `set -m`) is outside it and is killed only if the
  kill-time `ps` snapshot still finds it under the leader; one whose parent
  already exited is reparented away and not found, so `process_gone: true`
  attests to the leader, its group and the descendants that snapshot found, not
  to every process the CLI ever started. macOS has no subreaper, cgroup or PID
  namespace to close this; on Linux, `bwrap` runs add `--unshare-pid` and
  `--die-with-parent`, which should contain such descendants (not exercised).
  (b) A group is signalled by id. The kernel does not reissue the id of a group
  that still has a member, so a group kill can only be mis-aimed when the group
  is already completely dead and its id has been reissued to another group
  leader of the same user, which needs the pid space to wrap between the
  group's last exit and the kill; the code never looks a group up through a
  possibly recycled pid and never signals its own group. (c) Closing the event
  loop cancels the start itself, which reaches only the leader.
- **Other children the server holds.** The ACP client (`acp.py`), the Codex
  app-server client, the ZCode approval and StepFun RPC runtimes, the
  deliberation participant CLI (`cli_adapter.py`), the dispatch line runner
  (`auto_dispatch/execution.py`), the Feishu CLI call (`feishu_cli.py`) and the
  process scanner's `ps` and `lsof` runs (`agent_scanner.py`) start their child
  through `spawn_group` in `process.py`: the same held start as
  `stream_process`, so a cancellation that lands before the child is handed
  back, however often repeated, is held until it exists and its group is killed
  before the cancellation propagates. These callers carry no process tracker
  and take no `ps` snapshot (it blocks the event loop and nothing would record
  its result), so no stop acknowledgement rests on them. `spawn_group` always
  starts the child in its own session, so its group can be killed without the
  server's own. The Codex app-server, the deliberation CLI, the dispatch line,
  the Feishu call and the scanner's runs used to share the server's group (ACP,
  ZCode and StepFun already had their own session): a terminal interrupt sent
  to the server's foreground group no longer reaches them, and one the server
  leaves running when it dies without unwinding runs on. The Codex app-server
  exits when its stdin closes, which a dying server's pipe does (checked with
  the installed `codex app-server` and an isolated `CODEX_HOME`; its own tool
  children were not checked), and so does an idle `step --mode rpc` (checked
  with the installed `step`, no model turn run, so not one in the middle of a
  turn); the others run to their own end, since the timeouts that bound them
  live in the server.
  Their stop paths (`close()`, `cancel()`, a timeout, a cancellation) SIGKILL
  the leader's group by its id, the leader's pid, with no SIGTERM to the tools
  in it first, as `stream_process` does, so a tool in the middle of a write is
  cut off. The ACP and Codex clients' `close()` sends the leader SIGTERM and
  waits 3 s, then kills the group whether or not the leader left, because tools
  it started can outlive it; a cancellation during that wait does not skip the
  kill. A normal turn of those two lanes ends in `close()` while the agent is
  still up, so what it left running dies with it at the end of every turn; an
  agent that had already exited before `close()` gets no signal (see (e)). The
  ZCode and StepFun runtimes give the CLI 5 s to leave by itself when its turn
  ends and kill the group only if it did not or if that wait is cancelled (for
  ZCode that also still gives the provider lock and the permission folder back);
  an agent that leaves inside the 5 s, a run that ends on its own in the
  deliberation CLI, in the dispatch line and in the Feishu and scanner calls
  leave their group alone, as `stream_process` does. What ends a turn differs. A
  ZCode turn ends when the CLI's output closes, and its one-shot CLI is expected
  to exit then; the 5 s is for one that does not. A StepFun turn ends at
  `agent_end`, where the RPC server does not exit: `step --mode rpc` waits on
  its stdin for the next command (checked with the installed `step` while idle:
  it stayed up with its stdin open and exited at once, status 0, when the stdin
  closed; no model turn was run). The runtime therefore closes the agent's stdin
  first, which is how the server is told to go, and the 5 s runs from there. A
  StepFun turn that completed used to return at `agent_end` without stopping the
  agent at all, which was left running with its pipes open; only the agent's own
  exit or an explicit `cancel()` ended it. The CLI's own documentation says an
  `agent_end` can be followed by a retry or a compaction and marks the final
  settle with `agent_settled`; the runtime stops reading at the first
  `agent_end`, so what follows it is not delivered, and the stop after it may
  end it (not checked against a live CLI). The ZCode and StepFun runtimes used
  `getpgid(pid)` to find the group, which can name an unrelated group once the
  leader is reaped, and no longer do. A turn that is cancelled, that an error
  ends or whose stream the graph closes now stops the ACP and StepFun agent too
  (ZCode and Codex already did), and a StepFun turn that completes does as well
  (ACP, ZCode and Codex already did). Before, an ACP agent was stopped only by a
  completed turn or an explicit `cancel()`, and a StepFun agent only by its own
  exit or an explicit `cancel()`; in production only the Codex delegation calls
  `cancel()` (a worker stop, a command-approval timeout). A turn parked for an
  approval keeps its agent until it resumes, and the resume ends it the same
  way; nothing closes a runtime that is never resumed: the broker has no sweep
  and the server no shutdown hook. The Codex runtime closes its app-server when
  the `initialize` handshake is cancelled or fails, and publishes its client
  before the start so `cancel()` from another task reaches it; the ACP and Codex
  clients refuse a `close()` that lands while their process is being started and
  kill it instead of installing it. A dispatch line, a Feishu call and a scanner
  command that are cancelled are now killed; before, they kept running, and the
  timeout of the Feishu and scanner calls reached only the leader. The
  deliberation prompt is encoded before the CLI starts, so an unencodable one
  fails without a running child.
  Limits beyond (a) to (c): (d) these stop paths take no `ps` snapshot, so a
  descendant that left the group (`setsid`, `setpgid`) survives them. (e) A
  group is signalled by id, and the kernel keeps an id only while the group has
  a member or its leader is unreaped; once the leader is reaped and the group
  is empty the id can name another group leader's group of the same user. A
  handle held past its child's life (`close()` of the ACP and Codex clients,
  `cancel()` of ZCode and StepFun, a parked runtime held for hours) therefore
  signals nothing when the leader was already reaped as it began, and a tool an
  agent left behind when it died is not found by it. A kill bound to one call
  (a timeout or cancellation of the deliberation CLI, the dispatch line, the
  Feishu or scanner call, or of a start) can still run against a leader reaped
  earlier in that call, which is what it is for when a tool holds the pipes
  open: its window is the call, 180 s for a deliberation CLI by default, not
  the life of a handle; own group and ids 0 and 1 are refused. (f) The
  event-loop-close limit (c) applies as well. (g) `cancel()` of the ZCode and
  StepFun runtimes during their own start finds no process to stop and the
  start then keeps it; nothing calls those two today. Left alone: the `git`
  runs of the dispatch merge and push (`_git_command`), which a cancellation
  lets run to their end: killing a merge half way leaves its changes staged
  with no merge commit (seen on a scratch repository with a hook that slept),
  which is worse than the merge finishing, and a cancelled push goes on until
  git or its transport ends it; and the `docker` client of a Docker Worker
  (`docker_worker_runtime.py`), whose start is not held but whose container is
  removed by name.

Known limitations:

- A message with a credential key followed by `:`/`=` (a bare key word alone
  passes until its separator or a credential body arrives), a PEM block, an
  email, a terminal control sequence, a bidi override or isolate, a Unicode tag
  character, a lone surrogate, a URL password, or an unbroken token longer than
  512 characters (for example a very long URL) ends live text for that run; the
  final report is unaffected.
- The live view trails the Worker by the daemon's 0.2 s batching, the transport
  and the activity stream's pace (three tiers, see *Watcher cost*); a watcher
  that falls more than 2,048 events behind is reset rather than caught up;
  reasoning and answer text share one scan stream.
- Tool names and text share the sink's queue (256 events), which drains at 32
  events per 0.2 s, at most about 160 events a second. A Worker that emits tool
  records faster fills it with names (dropped names are only missing rows) and
  the next text event then finds it full and stops the stream for good
  (`buffer_overflow`, a `stop` event, watchers get `output_reset`; the report is
  unaffected). Each tool name is scanned against full windows, so such a flood
  also costs CPU linearly in the number of records, interleaved with the event
  loop instead of blocking it.
- Live streaming exists for the Codex provider only; the guard and the
  discontinuity wiring cover the daemon's Codex loop only (see *Only the
  daemon's Codex loop is guarded*).
- A descendant of the CLI that left its process group (`setsid`, `setpgid`) and
  the group-id reuse case are outside the stop proof (see *Stopping the CLI*).

### Enabling, limits and retention

- **Enabling.** Nothing to configure: a daemon streams when it runs the Codex
  provider, the server has migration 140, and `CONDUCTOR_DAEMON_TOKEN_STREAM` is
  not `0` (the default is on). `CONDUCTOR_DAEMON_TOKEN_STREAM=0` keeps only the
  report/activity summary; a server without the endpoint makes the daemon stop
  streaming on its first HTTP error (a 404/405/501 from an older server included).
- **Pattern-based, not a guarantee.** The guard is a heuristic over known
  credential shapes and key names: those of the shared sanitizer, URL userinfo,
  and (on the daemon) the values of the environment variables the worker policy
  names as credentials: a message that contains such a value, or ends in its
  first six or more characters (the rest could open the next message), is
  withheld and ends live text, so at most five characters of a value can be
  shown. A secret with no recognizable shape and no key word next
  to it (a random password in prose) cannot be detected by any scanner.
  - *URL rule, false positives.* Only the live text of that run ends and
    watchers get `output_reset`. Inside a URL: documentation placeholders and
    dev defaults (`user:password@localhost`, `postgres:postgres@localhost`,
    `<user>:<password>@<host>`, `${DB_PASSWORD}`), registry-less
    `docker://name:tag@sha256:...`, the source text of a URL regex
    (`https?://[^:@]+:[^@]+@`), `http://localhost:3000@evil.example` (a browser
    also reads `3000` as the password), a non-numeric or empty port with an `@`
    later in the same run (`http://host:abc/x@y`), a line of `word:word@word`
    right after a path-less URL line, and a lowercase `[redacted]` (only the
    exact marker is exempt). At the end of a joined message:
    `http://localhost:` followed by `$PORT`, `${PORT}`, `<port>`, `PORT` or a
    port of more than 5 digits (the same placeholders at the end of a line are
    clean, but refused when a path follows them and the message ends there), and
    a dot-less host name of 24 or more characters with a digit and a letter,
    such as a service name or a preview slug. Same precision class as the
    `password:` key rule; there is no allowlist.
  - *URL rule, what it does not catch.* Each case needs a message that ends
    exactly inside the URL, or a malformed URL: up to 5 digits of an all-digit
    password plus up to 3 closing characters (indistinguishable from
    `host:port`), a password that ends an event on a line break while its `@`
    opens the next event, the first character of a password that is `@` or `*`,
    up to 23 characters of a colon-less bare token (fewer for a known provider
    prefix), a raw `?` or `#` in the user name, a password of up to 5 digits
    followed by a raw `/ ? #` (a `host:port/@path` look-alike), a password
    containing a literal blank, a user name that starts with `[` and is all
    hex digits, colons and dots (read as an unclosed IPv6 literal), a
    single-slash `https:/user:pw@host`, and scheme-less forms (`user:pw@host:path`,
    `user:pw@tcp(host)/db`, scheme-relative `//user:pw@host`).
  - *Key names the rules do not know.* The credential-key rule and the sanitizer
    name the common long forms (`password`, `secret`, `token`, `api_key`,
    `auth`, `signature`, `access_key`..) and not the short ones, which are too
    common in ordinary text to refuse on sight (`key=`, `sig=`, `pass=`, `pwd=`,
    `PWD=` is the working directory). So these are stored: `?pass=`, `?pwd=`,
    `?key=` and `?sig=` in a query string, `Uid=sa;Pwd=...`, `AccountKey=...`,
    `export DB_PASS=...`, `MYSQL_PWD=...`, and command lines such as
    `curl -u user:pw`, `mysql -phunter2`, `redis-cli -a pw`, `sshpass -p pw`.
    The first characters of a credential of a known provider shape are still
    withheld whatever the name beside it.
  - E-mail addresses, and git SSH remotes with a dotted host
    (`git@github.com:org/repo.git`), were already refused by the sanitizer's
    e-mail rule (`redaction_candidate`) and still are; `ssh://git@host/...`
    without a dot stays clean.
- **Protocol limits.** At most 64 events per batch, 4,000 characters per text
  event (the daemon sends at most 2,000), 8 tool names (accepted up to 256
  characters, stored at most 64 after sanitizing), a 128-character run id, and
  a message of at most 64 KiB (longer stops the stream). Fence errors are 409s:
  `stale_output_epoch` (wrong epoch or lease generation),
  `duplicate_output_batch` (a replayed or older `batch_seq`),
  `bad_output_batch` (an allocation that does not start at `batch_seq` 1), and a
  plain "Lease is stale or Ticket is no longer running". A store failure is a
  200 with `stored: false`. A 200 always carries boolean `stored` and `tainted`,
  except the `stored: false` early return when no valid event remains.
- **Request validation errors.** A refused request is a 422 whose body is
  `{"detail": [{"type", "loc", "msg"}]}` and never carries `input` or `ctx`. This
  holds for every route, not only this one: an app-wide
  `RequestValidationError` handler (`src/server/app/setup.py`,
  `public_validation_errors`) replaces FastAPI's default, which echoes the
  rejected value (here possibly a credential or live Worker text). The web client
  reads only `loc` and `msg`; `ptc-cli` prints the `detail` it is given (its
  generic formatter) and so shows the reduced `{type, loc, msg}` entries, which
  read fine; the daemon discards error bodies. It is not value-free by construction: pydantic's UUID
  and union-tag sentences, which quote the value, are replaced, a `Literal` or enum
  sentence (`Input should be 'summary' or 'detailed'`) is the schema's own list
  and is kept whole, and `msg` and the rejected input cut out (when a string of 8 or more characters) and pass the
  shared credential-shape scrub, which is linear in the text; what is returned is
  then clipped to 128 characters per `loc` part and 1,000 for `msg`.
  `loc` is different: a name the caller chose (a dict key, an extra field, a
  union tag) is not the rejected value and can be a credential of no known shape,
  so no scrub is a guarantee. A `loc` part is kept only if the matched route's own
  schema defines it (its parameter names and the field names, aliases and literal
  tags of its body models, `route_schema_names`); any other string reads `<key>`.
  List positions, the leading `body`/`query`/`path`/`header`/`cookie` and
  pydantic's `[key]` marker are kept. A typo in an extra field therefore no longer
  names itself, which is the price of not repeating text nobody vetted. What
  remains is `msg`: a custom validator's own sentence is kept, but every run of 8
  characters that the request itself holds is cut out of it as well
  (`caller_runs`, `_cut_echo`). The request's text is its body keys and values at
  any depth, its query values (every value of a repeated key) and path
  values, lower-cased (ASCII and the lower-casing
  Python applies one character at a time; not full Unicode case folding), indexed
  once per request as a set of 8-character runs so a sentence is checked in time
  linear in its length. What is indexed is bounded: at most 2,048 nodes, 2,048
  characters of any one string (a longer value is indexed by its first 2,048
  characters only) and 16,384 characters in all, and at most 100 errors are
  returned. A validator that quotes text beyond
  those bounds, text that the exact cut and the run index both miss, or a secret
  it *transforms* (encodes, hashes, or splits into runs shorter than 8) therefore
  passes; the validators found doing any of that are value-free anyway (the BYOK,
  MCP, chat-role and chat-request-key ones, and `uuid.UUID`/`urlsplit` failures are
  not repeated). A sentence that shares a run of 8 with the request's text by
  chance reads `[REDACTED]` there. Routes that validate a body by hand and answer
  with the text themselves (the MCP save, import and plugin-update paths) use
  `validation_error_text(e, owned=...)`, which applies the same `loc` and sentence
  rules. Other bespoke validators that name the user's own keys on purpose, such
  as channel settings (`chats["slack:T1/C2"].workspace_id`), are not covered.
  The install report of a plugin package applies the same rules to the
  `extensions["ai.conductor"]` block it reads, with no request involved: a place
  is written with `public_path` and a pydantic finding with
  `validation_error_text(e, owned=..., under=...)`, so a key, an extra field or a
  map entry the package chose reads `<key>` (the finding keeps the block's own
  field names and the place the package wrote a list entry at, counted in the list
  as written, so an entry that does not validate and is dropped moves no other;
  a union's branch labels in a path, such as
  `skills`' `list[str]` and `dict[str,any]`, are not the schema's names either and
  read `<key>` as they do in a 422), and a validator's sentence is cut of runs of
  8 characters of the rejected value itself. The one name that is kept is a `servers` entry whose key is an entry of the package's own
  `mcp.json` **and a legal server name as written** (`declared_server_names`: it
  matches `NAME_RE`, is at most 64 characters and was not rewritten to fit), which
  the report lists as a component anyway. A key the install had to rewrite or
  could not use is any text, of any length, and reads `<key>`; a legal name is an
  identifier the user chose and is repeated even when it looks like a credential.
  The `target` of a finding about an entry is that entry's key, as it is for every
  entry finding and the component list, and is not covered by this. A bind or a
  server block that names nothing the document declares says that it is unknown,
  not which name: a bind says where the package wrote it in `secrets`
  (`secrets[1].bind[1]` is the second bind of the second secret, whichever entries
  before them did not validate and were dropped), and the blocks are one finding
  with a count. A bind that cannot land does not
  repeat its server, env variable or header name (its secret's name, which the
  block validated, stays). A namespace of `extensions` that is not an object reads
  `extensions.<key>` with its place among the package's namespaces, unless it is
  `ai.conductor`. A `regex` that does not compile is reported by its position
  only (`VaultBlueprint`): the engine's sentence quotes the pattern's own text,
  which a long pattern puts past what the cut indexes. An `icon_unusable` warning
  does not repeat the icon's path, since it fires when the path names no usable
  file and is then any text, and `unknown_root_key` and `cwd_ignored` do not
  repeat the root keys or the `cwd` they are about (the first counts the keys).
  A stored manifest that is parsed again with no report builds no sentence at all.
  The cost of the report does not grow faster than the number of findings in it,
  for the shapes that were checked (namespaces that are not objects, binds against
  the headers an entry ships, icons): where each of the package's namespaces is
  written is found in one pass, the headers an entry ships are read once per
  entry, and the names a report may repeat are settled once per package. The tests
  for this count those walks and compare two sizes of one package.
  Not covered: jsonschema's own messages about `plugin.json` and `mcp.json`, which
  quote the rejected value and an unexpected key, the fatal `name` pattern
  message, which quotes the name, the archive's file and directory names, and the
  names of the skills and subagents a package carries.
  The guarantee is therefore: the daemon output route's responses, every
  `RequestValidationError` 422, the MCP save/import/plugin-update messages, and
  the `ai.conductor` findings of a plugin install report, and the hand-written
  sentences described next. A route that checks a value itself
  and raises `HTTPException`, or returns a service's `ValueError` text, writes
  its own sentence. Those under `src/server/app`, and the services they take
  text from, were audited for a sentence that reads back a name, key, URL, path,
  provider, model, header, body field or id the caller chose and the server had
  not validated to a closed set. Such a sentence names the field and the rule,
  lists the options, or names the entry by its position; it does not quote the
  text, because a credential pasted into the field would come back in the
  response. Changed: the preferences' custom models and providers, per-model
  profiles (a profile is named only for a model the manifest ships or the
  account stored before the request, never for one the request itself adds),
  reasoning blocks (the
  validator keeps the path or level in `str()` for the manifest suite and
  returns a value-free `public` sentence to the route) and avatar content type;
  the BYOK provider in a delete, key-test, model-discovery or visible-models
  request; the key test's failures other than a vendor's HTTP answer, a
  timeout, an unreachable host and a host with no compatible endpoint, which say
  the kind of failure (the exception's class) and no longer pass on the HTTP
  client's own text, since that quotes the header it refused to send (a
  key pasted with a trailing space or newline into a provider that sends it in a
  header; with httpx 0.28.1 the refusal of a Gemini key, which rides in the URL,
  does not quote it); an autonomy Source's
  config (through `validation_error_text`, since `str()` of a pydantic error
  quotes `input_value`), a credential allowlist's stray keys and a schedule's
  timezone; a missing dispatch, dispatch line, feature, subagent, subagent to
  copy, native login provider, agent log, fleet agent or pip package; a
  subagent setting that is not one (the error's `field` reads `<key>`); an MCP
  tool binding, a plugin install's supplied secret names, a plugin update's
  package name and subdirectory, and a plugin source the egress guard or the
  transport refuses (the guard's `EgressBlockedError` keeps the host in `str()`
  for logs and carries a value-free `reason`, which is what the install says;
  the transport's `OAuthHopBlocked` carries a `HopRefusal`, and the install says
  which rule refused: a request for a host other than the one checked, nothing
  sent; a compressed answer; an answer past the size cap. A host written in
  punycode (`xn--`) is refused by the first of these rules on the transport as
  it stands, because the request reports the name in Unicode and it no longer
  equals the one that was checked; the same host written in Unicode is not
  refused by it);
  and the review queue's refusals (the `action` filter, a `line`, `reviewer` or
  `root` the request named, and what a root resolved to; a root that cannot be
  resolved, `~name` of an unknown user or with a NUL included, is a 400 rather
  than a 500).
  Named on purpose, and kept: a value the server parsed or matched (a UUID in
  the path, a dispatch, computer or workspace the caller owns and the server
  just loaded, an enum or `Literal` member, a provider the manifest ships, a
  feature the catalog registered); the user's own stored resource where its name
  is what they must act on (the custom provider a stored model still needs, the
  workspace that already holds a name, an installed plugin, a vault secret that
  already exists, a skill command that collides, the reserved folder a
  workspace name hit); the contents of an uploaded
  skill or plugin archive, which its validators quote through coded `params` so
  the pane can word the fix; the host, user and folder of an SSH machine, whose
  sentence exists to name the machine; the paths of a handoff manifest, which
  are how the Worker that listed them finds the entry to repair; a line a review
  refusal names once it is a worktree that exists under the server's root, and
  the line and branches the merge route names (it is loopback only); the path
  that keys each result of a bulk file delete; and a vendor's own answer to a
  key test, which is scrubbed by shape (URLs and a vendor's key prefixes): a
  vendor that quotes the key in a shape the scrub does not know comes back as it
  wrote it.
  Not audited: the exception text that a 500 or 503 passes through (`Dispatch
  could not start durably`, a thread replay failure, a manual Task's create
  error), which is infrastructure text and can quote what the failing statement
  held; the plugin package validators' messages, which quote values from the
  package being installed; and the MCP OAuth sign-in messages (a blocked hop, a
  refused authorization endpoint), which quote what the egress guard refused in
  a URL taken from the stored server or the vendor's metadata, a blocked hop cut
  to its host.
  `tests/unit/server/app/test_http_detail_echo.py` (and, for the review queue,
  `tests/unit/server/app/test_review_merge_api.py`, and, for the plugin
  transport's refusals, `tests/unit/server/services/test_plugin_fetch_refusals.py`)
  holds a regression test per changed family, which sends a sentinel where the
  field takes text and asserts that what comes back holds no run of 8 characters
  of it in either case, and one structural check. The check fails when an
  `HTTPException` detail, found under the name it is imported as, is built by an
  f-string, `+`, `%` or `.format` (directly, or through a local of the same
  function that is assigned such an expression) from a parameter of its function
  (or the item of a loop over one) that is not a UUID, `Literal`, number or flag,
  unless the formatted expression is listed there with its reason. It does not
  follow a value copied to a local, a detail made by a helper, `str(exc)` or the
  text of a service's exception, so a sentence of those shapes still needs the
  review this audit did.
  This route has no union, dict, `extra=forbid` or interpolating validator, so its 422 is
  value-free. A validator that checks a
  *dict* gets the whole dict as its rejected input, which the handler cannot cut a
  single key out of, so such a validator must not quote a key it was given: the
  BYOK `api_keys`/`base_urls` validators name a provider only when the server
  itself ships it (anything else reads "a custom provider"), and the MCP
  env/header validators name an entry by its position, never by its key.
  The handler must not log the exception: its text includes every rejected value.
- **Retention.** A Ticket epoch keeps its newest 2,048 events (a watcher that
  falls behind is reset with `gap`); allocating a new epoch deletes the older
  ones; everything cascades with the Ticket. Logs entirely older than a day are
  collected by the autonomy scheduler pass (the one advisory-locked pass per
  deployment), not by the write path, so a backlog can neither slow nor fail a
  run's first batch: first in each pass, at most 100 whole `(ticket, epoch)` logs
  per transaction (`EXPIRE_BATCH`), every 10 minutes
  (`OUTPUT_GC_INTERVAL_SECONDS`) and again on the next 2 s pass while a full
  batch says more remain; an idle pass is a single index probe. The statement
  judges each log by its newest row and filters before the `LIMIT`, so partly old
  live logs cannot crowd expired ones out, and it deletes whole logs, never
  single rows. A transaction advisory lock keeps two collectors (a lock
  hand-over) from deleting at once, and a failure is logged and waits out the
  interval. A log silent for more than a day under a live lease is collected too:
  its watcher is reset once with `gap` and the writer carries on; a watcher that
  attaches afterwards (no cursor, or one for another epoch) is handed the head and
  is reset at most for the epoch change, never for a gap it could not have lost. The scheduler
  is the only collector, so nothing is collected while it does not run. An append
  holds the claim row (exclusively) and the Ticket row (shared) for one short
  transaction (see *Session and epoch*).
- **Watcher cost.** The activity stream reads the log every 0.25 s while output
  flows (any cursor movement counts, an attach included), every 0.5 s once 3 s
  have passed without movement and every 1.0 s after 30 s
  (`ACTIVITY_STREAM_DAEMON_POLL_S`, `_MID_POLL_S`, `_IDLE_POLL_S`, with
  `_FAST_HOLD_S` = 3 and `_MID_HOLD_S` = 30). The first event after a pause
  trails by at most the delay of its tier, and the reads after it are fast again;
  a batch of 64 output payloads reads again at once and a terminal drain ignores
  the pace. A read selects only the session head (ticket, epoch, lease, run,
  `next_seq`, `tainted`; not the guard window and state, which are kilobytes),
  and a watcher already at the head runs no events query, so an idle watcher
  costs one small statement per read (one a second after 30 s of silence) beside
  the Ticket's own poll (three reads a second). Watchers are not capped per user.
- **Mid-deploy behaviour.** Until every worker runs this code, an old worker
  still locks the claim `FOR SHARE`, so it can still barge ahead of a waiting
  revoke (no worse than before), and still collects expired logs inside its
  first batch's transaction, where it can lock-wait against the new collector's
  delete (2 s `lock_timeout`) and answer that batch `stored: false`; the daemon
  then ends live output for that run (the report is unaffected, and so is the
  next run). No schema change is involved (migration 140 is unchanged).

## V1 scope

- manual requests from the Inbox composer;
- cron scheduled tasks (`/api/v1/autonomy/schedules`): a due schedule creates
  a manual Ticket, never a parallel execution path;
- GitHub polling of Issue comments and PR review comments (no webhook);
- Feishu chat polling through the host's official `lark-cli`: messages become
  Triggers, a message's root id becomes its Thread, and replies go back through
  the same Outbox;
- a Source may bind its Tickets to a native CLI Worker
  (`worker_provider`/`worker_model`); PTC remains the default;
- GitHub requests default to explicit: an allowed login must `@agent` in the
  comment. The Sources policy can switch to `observe` (run a read-only,
  plan-only Worker and wait for review) or `auto` (run the real work);
- Router Session in Home, restricted to autonomy tools (no Bash, files, web,
  subagents or MCP);
- Worker Session in the Source's workspace, one hidden thread per Ticket;
- follow-ups on a Ticket's Thread go directly to its Worker;
- results go back through the durable Outbox; replies are idempotent;
- attention / failed / completed Tickets are visible in the **Super-agent**
  page (`/superagent`);
- no automatic git push or pull-request creation in V1.

## Configuration

1. Store the GitHub PAT in the user vault (Plugins → Secrets) under a name,
   for example `GITHUB_TOKEN`. The Source stores only the name.
2. Enable the feature in Settings → Experiments.
3. Open **Super-agent → Sources**, add the repository, workspace, allowed
   logins and the vault secret name, then press **Check**.
4. On the next poll, an allowed user's `@agent` comment becomes a Trigger.
5. A Ticket appears in the Inbox; open the task to track or close it. The worker session is an execution log.

## Backend

- API: `/api/v1/autonomy/*` (`src/server/app/autonomy.py`);
- durable effects: `autonomy_effects`, claimed with `FOR UPDATE SKIP LOCKED`; the
  runner (5 effects at a time, in the process holding the pass lock) leases only
  as many due effects as it has free places, counting the handlers that outlived
  a stop (`stop()` waits up to 10 seconds for the pass loop and then up to 10 seconds for the handlers, and cancels none) when the same runner
  is restarted. An effect that waits for a place therefore holds no lease and
  spends no attempt, and a lease that runs out means a crash or a handler that
  outlived it, not a wait. A different process that takes the pass lock has 5
  places of its own, so for a while the deployment can run more than 5 effects at
  once;
- Workers: `AUTONOMY_MAX_RUNNING_WORKERS` (default 4) bounds the Workers the
  daemon-claim and cloud-launch paths start, exposed as `max_workers` in the
  metrics response and shown in Agents; the count is every `running` Ticket that
  is not orchestration-only, local-daemon and cloud Workers alike. A daemon
  claim and a cloud launch each count and make the Ticket `running` under one
  Postgres advisory lock (`WORKER_SLOT_LOCK` in `database/autonomy/tickets.py`):
  the claim holds it as a session lock around `claim_daemon_ticket`'s check and
  claim and only tries it, the launch takes it as a transaction lock inside the
  write that moves the queued Ticket to `running`
  (`update_ticket(slot_limit=...)`) and waits for it for at most a second,
  with that same second bounding its wait for its Ticket's row. Neither holds it
  across a container or a Worker run, and the wait holds no row lock, so it
  cannot close a cycle with the claim, Ticket and session row locks described
  under "Output stream protocol". A busy lock answers a claim that carries a
  claim id with a 503 (retry the same claim id), gives a claim without one no
  Ticket, and refuses a launch that has waited out its second. A full pool
  leaves the Ticket queued: a daemon polls again, and a cloud launch puts the
  newest live `launch_worker` effect of its Ticket back to pending, due in 10
  seconds and with its attempt given back (an older duplicate effect of the same
  Ticket returns and ends; see [CLOUD-WORKERS.md](CLOUD-WORKERS.md), "Concurrency", which
  also lists what the cap does not bound: a follow-up delivered to a Ticket sets
  it running without the cap, and the count has no per-user share; and that a
  launch holds one of the runner's 5 places for as long as the launch runs (the
  cloud timeout bounds only the container command), so five long Workers delay
  every other effect);
- pass: one advisory-locked pass per deployment, every 2 seconds; an Eval
  whose Router turn misses its 5-minute deadline is marked `unavailable` and
  its Triggers are put back in the Thread queue;
- tables: `migrations/versions/101_autonomy.py`, Watchdog Session in
  `migrations/versions/104_autonomy_watchdog_session.py`, Policy admission in
  `migrations/versions/105_policy_admission.py`, Eval retry counter in
  `migrations/versions/106_eval_expiry_retries.py`, handback flag in
  `migrations/versions/107_input_handback.py`, Ticket revision in
  `migrations/versions/108_ticket_revision.py`;
- Router tools: `src/tools/autonomy/`, host: `src/server/services/autonomy/host.py`;
- Watchdog review effect: `src/server/services/autonomy/watchdog_review.py`;
- GitHub Source: `src/server/services/autonomy/github.py`;
- Feishu Source: `src/server/services/autonomy/feishu.py` (subprocess path
  covered by a fake lark-cli integration test; live lark-cli auth still needs
  a host check);
- pipeline backlog metrics: `GET /api/v1/autonomy/metrics`
  (`src/server/database/autonomy/metrics.py`);
- Router health: `GET /api/v1/autonomy/evals` lists unavailable Evals and
  `POST /api/v1/autonomy/evals/{id}/requeue` puts their Triggers back; Sources
  shows the panel when there is something to fix.

## Router and Watchdog runtime (P)

The Router (`supervisor`) and Watchdog turns can run on the in-process P loop
(`src/server/services/superagent_runtime/`) instead of the PTC graph: no
sandbox session, no PTC graph or middleware, the same run ledger, Redis event
stream, checkpoint and finalize. Each role has its own deployment flag,
default `ptc`; Workers always stay on PTC. Design and Planner rulings:
the internal planning note `r8c-superagent-runtime-p` (not published).

| Role | Environment (wins over config.yaml) | config.yaml |
|---|---|---|
| Watchdog | `CONDUCTOR_SUPERAGENT_RUNTIME_WATCHDOG=p` | `superagent_runtime.watchdog: p` |
| Router | `CONDUCTOR_SUPERAGENT_RUNTIME_SUPERVISOR=p` | `superagent_runtime.supervisor: p` |

The flag is read once per admission, so a restart takes effect on the next
dispatch; a run already executing finishes on the runtime it started on. An
empty or unknown value means `ptc` (an unknown one is logged once). While
`supervisor=p`, Router snapshot mode is forced off.

A thread that PTC left unfinished (a pending interrupt, or a turn that died
mid-tool) is never continued on P: the probe before admission sends that turn
to PTC (`[SUPERAGENT_P] fallback=pending_interrupt`, or `fallback=probe_error`
when the checkpoint cannot be read), and a re-check after START ends a turn
that raced such a run with `error_type=superagent_pending_ptc` before any
model call (`[SUPERAGENT_P] pending_ptc_after_start`). A mid-turn credit stop
fails the run with `error_type=credit_stop` instead of pausing it.

### Flipping a production deployment

Watchdog first, then the Router. Both steps are a config change plus a
restart; nothing is migrated.

1. **Watchdog.** Add `CONDUCTOR_SUPERAGENT_RUNTIME_WATCHDOG=p` to
   the production checkout's `.env`, then run `cond restart`.
2. **Watch 10 clean watchdog turns** (Planner ruling 6). A watchdog review only
   runs for a Ticket with no attention set that has stalled past its threshold
   (queued, waiting or running), at most once per six hours per Ticket, so a
   quiet watchdog during this watch is normal and it can take a while. For each turn check:
   - `cond logs -n 500 backend` shows one
     `[SUPERAGENT_P] thread_id=… role=watchdog model=… total=…ms (db_setup=… model=… lane_build=… workflow_start=…)`
     line, with no `session` or `graph_build` phase and no sandbox start for the
     Home workspace;
   - no `[SUPERAGENT_P] fallback=probe_error`, no
     `pending_ptc_after_start`, and no `[PTC_ERROR]` or `[LocalRunExecutor] Workflow … failed`
     line for the watchdog thread (`fallback=pending_interrupt` is expected
     at most once per thread right after the flip: that turn ran on PTC);
   - the run row is `completed` and stamped: a read-only query on
     `conversation_responses` where `metadata->>'superagent_runtime' = 'p'`
     shows `status = 'completed'`, with no `error_type` in its metadata, and a
     `conversation_usages` row for the run;
   - the decision landed: `autonomy_watchdog` answered `continue` (only the
     review stamp `watchdog_reviewed_at` moved) or `attention` (the Ticket's
     attention was raised; Ticket activity on `/tasks/:ticketId`);
   - the `chat_turn_phase_duration_ms` histogram has samples with
     `mode="superagent_p"`; compare its START-to-first-frame and
     START-to-`run_end` medians with `[PTC_TIMING]` turns (PRD acceptance 5).
3. **Router.** After 10 clean watchdog turns, add
   `CONDUCTOR_SUPERAGENT_RUNTIME_SUPERVISOR=p` to the production checkout's `.env` and run
   `cond restart`. Check the same lines with `role=supervisor`, that
   `autonomy_route` decisions and replies keep arriving, that Evals do not
   start expiring (`GET /api/v1/autonomy/evals` stays empty), and that
   `conversation_usages` rows are written. Run the 0→1 end-to-end test with
   both roles on `p` before R9b starts deleting PTC code.

### Rollback

Remove the variable from the production checkout's `.env` (or set it to `ptc`) and run
`cond restart`. The next turn of that role runs on PTC; P wrote only standard
messages to the checkpoint, so PTC reads the thread as is. The first Router
turn after a rollback cold-starts its sandbox session again, which is
expected.

## Roles, assignment and soft gates (R6 / N73)

**Role registry (R6 L1).** One configuration holds the four roles -- the
Super-agent (Router), the planner, the reviewer and the worker -- each with
candidates in the owner's preference order (`name` or `name@version`), a
default and constraints. Read and edit it with `GET/PUT /api/v1/roles`,
`conductor roles show|set|audit`, the TUI `/roles` command and the web
console (Settings, Roles). Reading is open to signed-in users; a change is
deployment-wide, so `PUT /api/v1/roles/{role}` and the Router handoff need the
operator credential (`X-Service-Token`, which `conductor roles set` sends from
`INTERNAL_SERVICE_TOKEN`); the TUI and web console show a refusal without it.
Edits are bounded: per-role `extra` keys are allow-listed, sizes are capped,
candidates must be known roster names and reviewers must be read-only capable.
Every change needs a reason and is written to `role_registry_audit`
(migration 151) in the same transaction. A change applies to the serving
process at once; other processes pick it up when they start or refresh. With nothing
stored, the legacy settings apply exactly as before: `config.yaml`
`autonomy_router_model`, `CONDUCTOR_DISPATCH_PLANNER` and
`CONDUCTOR_ROLE_POLICY_FILE`. The hard rules -- the Router routes and never
executes, review is read-only, an author never reviews its own work, a merge
needs a human -- rest on the runtime code (no execution tools in the Router
session, read-only review adapters, Stage-Gate's author bar, the human
gate); the registry's flags only describe a role and are validated against
those rules.

**Dynamic assignment (R6 L2, L4).** Per line, the stored worker entry
excludes non-candidates and names lacking a required capability (session
resume), and its order is a soft preference. The selector can sample by
per-category success x headroom (`CONDUCTOR_DISPATCH_SELECTOR_MODE`; unset is
shadow with a stored registry and off without one, so an empty registry
selects exactly as before); at a repeated debug point a different model takes the line in a new
session (`CONDUCTOR_DISPATCH_DEBUG_SWITCH_MODE`, default shadow).

**Router handoff and service roles (R6 L3).** `POST /api/v1/router/handoff`
switches the Router to another router candidate (for example on quota).
Context is scoped to each Router session: the store keeps only a digest and
the Spill references per session, never the Inline Ticket rows, and each
session's next turn gets a one-time note with its own references (delivery
is recorded on that Router thread's metadata, outside the registry's revision
chain, so a restart does not repeat it and no revision or audit row is
written). `GET /api/v1/roles` and the audit show only the number of sessions a
handoff carried, not their thread ids or references. `handoff_on_quota` exists but
is not wired to a quota signal yet. With a stored registry (or
`CONDUCTOR_DISPATCH_SERVICE_CHAIN=1`) a live dispatch records its
plan -> assign -> review chain in `gates.service_chain`; reviewer entries
never carry the verdict.

**Super-agent assignment (N73).** After planning, one structured
`LLMService.complete` call (background slot, the Router's model) picks each
unassigned line's model, effort and runtime. The dispatcher only checks
feasibility (eligible and `can_take_line`, CLI on PATH, not disabled or
skipped or breaker-open, headroom or paid credits, never the line's
reviewer; claude and codex never author). An infeasible line is asked once
more with its reason code, then falls back to the selector; an ERROR is never
a choice. Every decision is recorded on the line (`gates.lines[id].assignment`,
with the selector's counterfactual) and shown by `conductor dispatch status`,
`conductor dispatch assignments ID` and `GET /api/v1/dispatch/{id}/assignments`.
An adopted choice reaches the launcher as `CONDUCTOR_LINE_MODEL_ID`,
`CONDUCTOR_LINE_CLI_EFFORT` and `CONDUCTOR_LINE_RUNTIME`.

Rollout (Q8): unset `CONDUCTOR_DISPATCH_ASSIGN_MODE` means shadow (the
Super-agent's choice is recorded, the selector's pick runs). Only an explicit
`CONDUCTOR_DISPATCH_ASSIGN_MODE=auto` starts the staged rollout: shadow for
30 lines, then a 50% sampled enforce until 40 lines ran on the Super-agent's
choice, then full enforce. After that it rolls back to shadow by itself when
the Super-agent cohort's pass rate is more than 10 points lower or its cost
per line more than 50% higher than the selector cohort (10 lines per cohort
minimum), checked in the 50% stage as well. The stage is stored in
`dispatch_assign_rollout` (migration 151) and only moves forward; a rollback
is sticky until an operator clears that row. `off`, `shadow` and `enforce`
(with `_SAMPLE`) are used as given. In shadow every assignment makes one
Router-model call; `CONDUCTOR_DISPATCH_ASSIGN_SHADOW_CALL=0` skips it.

**Soft gates (N73 S1-S8).** Paid credits are on by default
(`CONDUCTOR_DISPATCH_ALLOW_PAID_CREDITS=0` turns them off). The developer
pool and DAG quota are inputs, not gates (`CONDUCTOR_DISPATCH_QUOTA_ADVISORY=0`
restores quota placement). N46, N47 and N52 are record-only
(`CONDUCTOR_DISPATCH_RECORD_ONLY_CHECKS=0` restores enforcement); their
findings, the assignment record and the scope (declared, touched, outside,
high-risk) are in the review evidence, and the Reviewer must answer each.
With `CONDUCTOR_DISPATCH_AUTO_APPROVE=1` (off by default), a low-risk
dispatch -- G2, non-author MERGE and G5 passed, complete changed-file
evidence, no scope, worktree or record-only finding, no high-risk path -- is
marked `auto_ready` by `auto:n73` (`gates.auto_approval`). That only says the
local integrate branch is ready: the approval gate stays pending, the gate
refuses an automatic approver, `GitMerger.push` refuses one, and delivery
refuses an `auto_ready` dispatch without a human approval. Push, PR and merge
to main stay human steps. INCONCLUSIVE, ERROR, unparseable replies, parks and
human unblocks are never marked.

## Current limits

- V1 has a deterministic stall watchdog (`watchdog.py`): it flags stale
  queued/waiting/running Tickets, skips a running Ticket with a recent Worker
  progress checkpoint, and marks a running Ticket failed when its run is no
  longer live. A long-running Ticket then gets one model review in a separate
  hidden Watchdog Session (`platform="superagent_watchdog"`); its
  `autonomy_watchdog` decision can clear the attention or ask a sharper
  question, and the review stamp throttles repeats for six hours.
- Acceptance modes are explicit/observe/auto. Observe runs a read-only,
  plan-only Worker; Retry executes the approved plan. Auto runs the real work.
- Workers are PTC analysts or native CLI Workers; automatic PR writes still
  come later.
- The Router, Worker and Watchdog threads are debug views, not user-managed
  sessions.
- BYOM: a manual Ticket can name a local daemon (`worker_daemon_id`); the
  daemon claims the Ticket and runs its native CLI on the user machine, while
  the cloud keeps the Ticket and outbox. See `docs/SUPER-AGENT-BYOM.md`.
- The Router batch carries a bounded Thread snapshot (12k chars) and lists
  spilled Trigger/Ticket ids; autonomy_read(thread) returns the full rows.
- The Policy admission table maps listed/unlisted senders, addressed vs not, and bots to explicit/inferred/ignore; an inferred strength follows the accept mode. A Source can override selected cells in its own config; invalid overrides fall back to the Policy.
- Saving a Policy change discards active Evals (unavailable, reason
  `policy changed`) and queues their Triggers again; the next pass starts them
  under the new Policy version.
- Each Source has a bot allow-list; a bot outside it is dropped before the admission table. A retry revalidates the Ticket's Mandate against the current Source, sender admission and accept mode; a revoked Mandate cancels the Ticket instead of launching a Worker. An Eval that misses its deadline is retried twice; the third expiry leaves the batch unavailable and stops automatic re-evaluation. A Policy change also stores a policy notice Input on every open Ticket and delivers it to a running Worker. A Worker can hand a follow-up Input back to Eval with `autonomy_handback`; the input id rides on the delivered follow-up. The Trigger is not requeued while another live Ticket still subscribes to the Thread. A follow-up on a Thread goes to the Ticket that opened that Thread before any attached subscriber. A Worker can follow a related Thread for context with `autonomy_follow_context`; those messages become stored context Inputs and grant no authority. Immutable Trigger reads use a bounded process-local TTL cache (60s). Ticket details use a 15s cache keyed by the Ticket revision, which every Ticket/Input/Report write increments; Thread snapshots stay uncached. Still missing: full multi-owner context semantics and BYOM.
