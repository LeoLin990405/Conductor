# `cond` Super-agent TUI

Merged from `feat/cli-tui` (merge `27c7dec0a`, an ancestor of this checkout)
and extended since by the daemon live output stream, the reset/epoch fences,
the hidden-character sanitizer and names-only tool blocks (all described
below). The full-screen TUI defaults to the **Supervisor view**: it draws the
Super-agent's phase, progress and final result, but not the Worker's token
stream or tool names. Worker output is still consumed (and still passes the
same sanitizer and continuity guard), so `/details` can reveal it without
changing the Ticket. The Worker detail is an inspection aid, not the product's
main surface.
The full-screen TUI is a prompt_toolkit application over the existing
autonomy Ticket API; it added no new dependency (`prompt-toolkit` and `rich`
are declared by `libs/ptc-cli/pyproject.toml`) and, when it merged, no new
backend endpoints except one read-only overview field.

## Modes

`cond` (which runs `ptc-agent --super-agent`) chooses a mode from the terminal
and the flags. The rows are in the order the checks run (`runner.py`,
`choose_mode` in `tui.py`), so when several apply the first one wins:

| Condition | Mode |
|---|---|
| `--exec COMMAND` | run one command through the line shell and exit (unchanged) |
| `-p/--print PROMPT` | **one-shot**: create one Ticket, follow it, print the result, exit with its outcome (0 completed, 1 otherwise); see [Print mode](#print-mode) |
| `--no-tui`, `CONDUCTOR_NO_TUI` set to `1`/`true`/`yes` (any case), stdin or stdout not a TTY, or `< 80` columns | **line shell** (the Rich `SuperAgentShell`) |
| otherwise | **full-screen TUI** |

The width is read once at launch (`shutil.get_terminal_size`, 80 columns when
the size cannot be read, which still gives the TUI), so resizing a running TUI
never switches modes. `--no-splash` leaves out the welcome panel.

`--continue` reopens the most recent persisted session in the TUI. In the line
fallback it restores the session's daemon and prints one line,
`resumed session <id> (line mode; last Ticket <first 8 characters>; daemon
<name or auto>)` (the line shell has no transcript). `-p` ignores it.

## Print mode

`cond -p "prompt"` creates one Ticket on Home (the daemon is chosen as in
[Commands](#commands); `@file` mentions in the prompt are attached from the
current directory, and a refused one prints `attach error: ...`), follows it
for at most the 15-minute watch, and writes plain lines to stdout:

- `ticket <id>` first, then the Worker's `text` output as it streams (chunks
  are joined on one line; reasoning is not printed);
- before any non-text line the open text line is ended, then `tool: <names>`,
  `• <kind> <summary> — <detail>` for an activity row, `worker error: <text>`,
  the gap text, `live output restarted (<reason>)`, `watch timed out`;
- the end: `status: <status>`, the summary, and `needs you: <question>`.

Reconnect and polling notices are not printed. The exit code is 0 only when the
final status is `completed`; any other end (`failed`, `cancelled`, `blocked`,
`waiting`, `archived`), a timeout, no Home workspace, no registered daemon, a
malformed server answer or any error returns 1. Streamed text goes through the
incremental sanitizer (see [Terminal safety](#terminal-safety)), so the output
carries no terminal controls.

## Screen

An illustration of the layout (not a captured screen), top to bottom as
`build_application` stacks the windows:

```
┌ conversation ─────────────────────────────────────────────────────────────────────────────────┐
│ > fix the failing importer test                                                                │
│ · ticket 1a2b3c4d created (queued)                                                             │
│ the failure is in the legacy parser…                                                           │
│ ⚙ read_file [done 1.2s]                                                                        │
│ the fix, as the assistant wrote it:                                                            │
│ --- a/src/legacy/parser.py                                                                     │
│ +++ b/src/legacy/parser.py                                                                     │
│ @@ -41,2 +41,2 @@                                                                              │
│ -    return parse_v1(line)                                                                     │
│ +    return parse_v2(line)                                                                     │
│ • report completed — importer test now passes                                                  │
│ status: completed                                                                              │
├ permission row (only while a local action is asking) ─────────────────────────────────────────┤
│ conductor merge feat/x --into main (local-merge) — merge feat/x --into main                    │
├ composer ─────────────────────────────────────────────────────────────────────────────────────┤
│ superagent> _                                                                                  │
├ status bar (one row) ─────────────────────────────────────────────────────────────────────────┤
│ model deepseek-flash-account │ jobs 3 │ local (online) │ ~1204 tok │ session 1a2b3c4d │ live   │
├ hint (one row) ───────────────────────────────────────────────────────────────────────────────┤
│ Enter send · Shift+Enter newline · @file attach · /help · Esc stop watch · Ctrl+C twice quits  │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

While a permission is pending the hint row is replaced by `permission needed:
<the permission row text> — [a]llow once  [s]ession allow  [d]eny`.

- **Conversation window** — scrollable (`PgUp`/`PgDn` move 10 rows; there is a
  scroll bar and no mouse support); it follows the tail until the operator
  scrolls up, and returns to the tail when paged past the end. Each block is
  rendered as its own row set with a cursor marker on the visual row the view
  should show; the control pre-wraps every logical row to the content width, so
  a long wrapped paragraph is paged through row by row instead of jumping
  between its first and last visual lines. Blocks are typed: user prompt (with
  attachments), streamed assistant text, dimmed reasoning, `▸ sub-agent <name>`
  groups when the Worker labels its output, activity rows (`• kind summary —
  detail`), tool blocks, notices (`· ...`), errors, the end state (`status:
  ...`, the summary, `needs you: ...`) and permission rows. When more than 800
  blocks accumulate, the oldest activity and notice rows are dropped first; user,
  assistant, tool, end and error blocks are kept.
- **Composer** — fixed below the conversation, 1–8 rows (3 preferred), prompt
  `superagent> `, persisted history at `~/.ptc-agent/superagent-tui-history`
  (every non-blank accepted line is appended, commands and `!` lines included;
  Up/Down recalls them at the first/last row; the line shell keeps its own
  `~/.ptc-agent/superagent-history`), `/` and `@` completion.
- **Status bar** (a single row, plain text) — Router model (from `GET
  /api/v1/autonomy/overview`; `unknown` until a value arrives), active Jobs
  (`running + queued` from the overview counts, at least 1 while a watch runs),
  the selected daemon with its liveness (`online` when any registered daemon
  was seen in the last 180 s, otherwise `stale`; the name reads `no daemon`
  when the listing is empty or the request fails). The `(online)`/`(stale)`
  suffix is set only from a non-empty listing and is cleared by a session
  switch, not by a later empty listing, so after the last daemon disappears the
  bar reads `no daemon (online)`; it is a bare `no daemon` only when no daemon
  was ever listed in that process (reproduced with a fake client). Then a
  conversation token estimate (CJK ≈ 1 token, other text ≈ 4 chars/token; it
  counts the prompts, the assistant text without reasoning, and the tool names
  plus any output lines a restored tool block carries, and is an estimate
  only; the welcome panel is an assistant block, so a launch with it already
  shows a nonzero value, 121 in one run here, depending on the width), the
  session id, and the transport
  state (`idle`/`live`/`polling`/`reconnecting`). The overview is re-read at
  startup, when a watch ends and on `/model`, so the Jobs count is not live.
  The Ticket's own status is not in the bar; it shows in the `status:` row when
  the Ticket ends.
- **Tool blocks** — `⚙ <name> [<status> <duration>]`. A tool block shows the
  name only: its arguments and file bodies are never projected (they cannot be
  made credential-safe), the client reads nothing but the `tools` names (and
  the run and epoch) from a `tool` frame, and the `params`/`diff` keys of a
  saved session file are not read back, so nothing of them is drawn. A block
  is `running` until the next event closes it (the next activity row, status,
  reset, gap, error, end or timeout, or the next tool call), then `done`, or
  `failed` after a worker error or a failed end; the duration is the client's
  own clock from the name arriving to that event, not the tool's runtime. The
  block has room for output lines (capped at 200, 12 shown with `… N more
  line(s)`), but nothing in `ptc_cli` feeds them in this checkout (`add_line`
  has no caller), so they appear only when a saved session file carries
  `lines`; for a block without lines `Ctrl+T`/`Ctrl+O` change nothing visible.
  A unified diff the assistant writes in its own text is still colored, but
  only a real one: it needs a file header (`diff --git` or a `---`/`+++` pair)
  and an `@@ -n,m +n,m @@` hunk header, otherwise the `-`/`+` lines stay
  ordinary text (`parse_unified_diff` returns nothing for them).
  Sub-agent blocks (the stream's `agent` field) are grouped and labeled
  separately, and a new generation (`run` id) starts a fresh assistant block.
- **Rich content** — prompt_toolkit owns the screen, keys and scrollback;
  Rich renders the welcome panel, the `/help` panel and the `/model` provider
  table as plain captured box layout (sized to the terminal width minus 4, at
  least 40), so they scroll with the transcript and cannot draw over the
  composer. Every rendered block is split to one logical line per row, which
  keeps the scroll marker aligned even for multi-line prompts, tool output or
  panels.

### Supervisor view

`GET /api/v1/autonomy/tickets/{id}/progress` is the calibration source for the
Supervisor phase, elapsed time, summary, question and error. SSE stays
responsible for liveness (the header dot and `live` / `polling` /
`reconnecting`).

Progress is fetched once at each of these moments: right after submit, when
the Ticket id is first confirmed; when an SSE `status` state changes or an
`end` frame arrives; after an SSE reconnect succeeds (including when the first
frame back is an activity row); and when the operator runs the read-only
`/status` command. The 0.5s refresh heartbeat redraws the header and does not
call progress. `/details` is unchanged.

From the stream, phase order is `queued` < `running` < `waiting` < a terminal
phase. The terminal phases `completed`, `failed` and `cancelled` are peers:
SSE must not switch from one to another or leave a terminal. A progress
response that is still current may set any phase, including a move backwards.

Each progress read is bound, before it waits, to the Ticket it requested, the
view generation, and a request sequence. The generation advances when the
viewed Ticket changes or is cleared (a new progress card, `/new`, `/clear`,
or a restored session). The sequence advances at the start of every progress
read and is what orders two reads of the same Ticket. When the response
arrives, that HTTP body is dropped if the session epoch, that Ticket, the
generation, or the sequence is no longer current. A dropped body does not
change the phase, elapsed time, summary, question, error, source mark, or
the current result card. That includes a successful payload, HTTP 404, an
empty or non-object body, and a failed request. `CancelledError` still
propagates. Dropping the body does not drop the SSE frame that triggered
the read. The frame is still merged when it belongs to the current Ticket
and view generation, so a newer `/status` that 404s or errors cannot swallow
the Ticket's `end`. A newer read that succeeds still wins: the older frame
does not rewrite the snapshot that success applied.

A progress read that is still current and fails (HTTP 404 — the feature is
off, or the Ticket is not owned by the current user — or any other error)
leaves the SSE-derived phase, elapsed time, summary, question and error in
place. The Supervisor header then shows `source stream`. A successful current
calibration shows `source server`.

On the progress card, and on a result card that calibration overwrites,
`needs you: …` appears as soon as the phase is `waiting`, and `error: …`
appears as soon as the phase is `failed`. Neither waits for an end frame.
`summary: …` is shown only for a terminal phase (`completed`, `failed`, or
`cancelled`), not while the phase is `queued`, `running`, or `waiting`. The
fields are still stored. An end card applies that filter every time it is
drawn, including while a progress read is still in flight, after the watch
is cancelled, and after the session is restored. It does not wait for
calibration to erase a non-terminal summary. Field visibility for `blocked` follows
the `waiting` rules and field visibility for `archived` follows the `cancelled`
rules. The Ticket report body (migration 144) is shown once on the EndBlock,
and only at a terminal phase (`completed`, `failed` or `cancelled`). A
non-empty body replaces the summary; the summary is the fallback when the
body is empty, and the same text is not drawn again beside those rows. A
non-terminal phase leaves the body off the card. EndBlock status and title
stay the original values. The view does not render a Worker transcript
or tool arguments.

## Keys

| Key | Action |
|---|---|
| Enter | send the line (a blank line is ignored) |
| Shift+Enter | new line when the terminal reports it (kitty CSI `13;2u` / xterm `27;2;13~`); everywhere else `Ctrl+J`, or `Esc` then `Enter` pressed quickly (Esc flushes after 0.1 s) |
| `@path` + Tab | complete/attach a local file |
| `/` + Tab | complete a command |
| `PgUp` / `PgDn` | scroll the conversation by 10 rows |
| `Ctrl+T` | expand/collapse the newest tool block |
| `Ctrl+O` | collapse all tool blocks, or expand all when none is expanded |
| `Esc` | deny a pending permission immediately; otherwise stop watching the current Ticket (the Ticket keeps running) |
| `Up` / `Down` | recall composer history at the first/last row; move the cursor inside a multi-line draft |
| `Ctrl+C` | clears a non-empty composer; also denies a pending permission and stops watching the current Ticket (the Ticket keeps running); the first press adds the notice `press Ctrl+C again to quit`, a second press within 1.5 s quits |
| `Ctrl+D` | quit when the composer is empty |
| `a` / `s` / `d` | answer a pending permission (allow once / session allow / deny) |

While a permission is pending, the TUI's Enter, Up/Down and newline bindings
are switched off and `a`/`s`/`d` answer it instead of typing.

## Commands

| Command | What it does |
|---|---|
| `/help` | the key map and this command list, as a Rich panel in the conversation |
| `/jobs` | list up to 10 Tickets (`limit=10`, in the server's order), one `NN. <id8> <status> <title>` notice each; the number is the index `/jobs` and `/stop` accept |
| `/jobs <id\|index>` | Ticket detail: title, status, worker, update time, the `needs you` question and the summary; add `watch` (or `follow`) to follow it afterwards |
| `/jobs watch <id\|index>` | follow a Ticket live |
| `/stop <id\|index>` | request a stop |
| `/agents` | the local fleet (id, role, status, branch) and the registered daemons |
| `/signals` | the Sources and the Job counts |
| `/model` | the Router model and the configured providers/models |
| `/review <args>` | run `conductor review <args>` locally (`run <line> --by <line>`, `status [line]`, ...); every form is gated |
| `/merge <line> --into <branch>` | run `conductor merge ...` locally; further arguments (such as `--test-cmd`) are passed through; gated |
| `!<command>` | run a shell command in the launch directory; gated |
| `/clear` | clear the conversation (the next save writes the empty transcript) |
| `/details` | toggle the Worker output/tool stream (default: Supervisor view) |
| `/compact` | drop notice rows and consecutive duplicate activity rows; sealed-fragment boundaries stay |
| `/expand`, `/collapse` | expand / collapse every tool block |
| `/resume [id]` | list the newest 10 saved sessions, or switch to one |
| `/new` | start a fresh session |
| `/exit` | save and quit |

Aliases: `/quit` and `/q` for `/exit`, `/j` for `/jobs`. Slash arguments are
split on whitespace, with no shell quoting. The TUI has no
`/jobs retry|reply|requeue|new`, no `/jobs --status|--attention|--failed|--limit`
filters, no `/agents logs` and no native-runtimes table (the `/agents`
completion hint still says "native runtimes"); those exist only in the line
shell (`--no-tui`).

Plain text creates a manual Ticket on Home (title: the first line, 120
characters at most) and follows it live. The daemon is chosen like the line
shell: among the daemons seen in the last 180 s, or all of them when none was,
the most recently seen first; the choice is kept for the session. With no
selected daemon, after re-reading the daemon list once, the TUI shows the error
`no local daemon is registered; the deployment cannot run cloud Workers.
Register and start one (conductor daemons).` and sends nothing. That wording is
fixed: the TUI never creates a Ticket without a daemon id and does not check
whether an operator enabled the opt-in cloud Worker runtime (off by default),
so it refuses in that case too. Print mode behaves the same (`no local daemon
is registered; cannot run a Ticket`, exit 1). The line shell differs: it prints
a warning at start and still sends the Ticket without a daemon id (what the
server then does with it is not verified here). Commands that mutate this
machine are gated (below). Local actions run the same
`python -m conductor_cli --url <server> …` subprocess the line shell uses, with
`CONDUCTOR_WT_ROOT` exported (default `~/conductor-wt`, which is also the
subprocess's working directory when it exists). The TUI gives
`merge` 11,100 s, other `conductor` commands 3,600 s and a `!` command 600 s;
the line shell gives every `conductor` command at least 11,400 s, more when
`CONDUCTOR_MERGE_TIMEOUT_SECONDS` is set (that value plus 120 s, up to 90,000
s). The TUI shows only the last 40,000 characters of a local action's output.

## Live events

One Ticket is followed through
`GET /api/v1/autonomy/tickets/{id}/activity/stream`:

- `activity` frames become activity blocks (deduplicated by `event_id`);
- `output` frames: `text`/`reasoning` stream into an assistant block
  (terminal controls and hidden characters are stripped: by an incremental
  sanitizer in the line shell and print mode, since an escape sequence can split
  across frames, and by `plain()` over the whole block when the full-screen TUI
  draws it, which keeps the raw text; see
  [Terminal safety](#terminal-safety)), `tool` creates tool blocks, `error`/`gap`
  become error/notice rows. A `gap` (dropped output) is a discontinuity like a
  reset but without a second notice: the TUI seals the current assistant block
  and closes running tool blocks; the line shell and print mode also start a
  fresh sanitizer, and the line shell forgets the run and epoch. Local daemon
  output also sends `output_reset` (`new_epoch`, `continuity_lost`, `gap`,
  `cursor_invalid`) and an `epoch` on every daemon payload (cloud run frames
  carry none): a reset seals the current assistant block (the next chunk always
  starts a new one; the sealed block renders its own `⟲ live output restarted`
  boundary line, which `/compact` and trimming keep; the line shell and print
  mode also drop the incremental sanitizer's half-parsed escape state, which
  the TUI has none of), closes the output line, closes running tool blocks and
  adds a dim `live output restarted (...)` notice. A changed (run id, epoch)
  pair without a reset, on a text chunk or on a tool name, is treated as one
  (the line shell compares the same pair). The watch never joins text across a
  reset, and the cursor stays opaque (the server re-derives the position on
  reconnect);
- `status` frames draw nothing and do not touch the bar; a terminal status
  (`completed`, `cancelled`, `failed`, `blocked`, `waiting`, `archived`) is
  remembered for `end` to fall back on, and it is *not* the end of the stream
  (a retry can revive the Ticket);
- `end` settles the conversation; `timeout` (the server's 15-minute ceiling,
  `ACTIVITY_STREAM_MAX_S`) ends the watch.

Live model text appears only when the Worker publishes it: a local daemon
running the Codex provider (server at migration 140, and the daemon's
`CONDUCTOR_DAEMON_TOKEN_STREAM` not turned off; it defaults to on), or a Worker
run that publishes run events. Other native providers and the Docker-isolated
cloud runtime give the activity rows and the final report instead; see
`docs/CLI-SUPERAGENT.md` (Known gaps) and `docs/SUPER-AGENT.md` (Output stream
protocol).

An attach that has delivered at least one frame reconnects with
`Last-Event-ID` after a transport failure, after an SSE `error` frame
(`live stream lost (...); reconnecting`) and after a clean close with no `end`
frame (`reconnecting the live stream`). An `error` frame counts as a frame even
when it is the first one of an attach, so it reconnects instead of polling at
once. All reconnects share one budget of five backoff attempts (0.5, 1, 2, 4, 8
s) per watch (not refilled when frames resume); the sixth failure polls. Any
failure before the first frame (a deployment without the stream: 404/405/501, a
non-SSE body or another HTTP error), a reattach that closes without a frame, or
a reconnect that fails before delivering a frame falls back to polling
`GET …/{id}` + `GET …/activity` every 2 s until the Ticket is terminal; a
failed poll shows a `polling failed (...); retrying` notice and is retried until
the watch deadline. Each attach and each poll is bounded by the watch deadline
(15 minutes, `WATCH_TIMEOUT_S`), so a stalled stream cannot outlive the watch,
and a timeout says the Ticket is still running. The bar says
`live`/`reconnecting`/`polling`, and `idle` when no watch runs. Starting a
second watch (new prompt, `/jobs watch`) stops the previous watch with a
notice; the previous Ticket keeps running. `/new` and `/resume <id>` cancel and
join the in-flight watch before switching sessions, so late frames can never be
written into the wrong session.

## Terminal safety

Everything a Worker can influence is drawn through
`libs/ptc-cli/ptc_cli/superagent/text.py`: `plain()` / `text()` for a whole
string and `TokenSanitizer` for a stream (`text()` is `plain()` plus Rich-markup
escaping, for the line shell). They are **one scanner** (`_scan`) used two ways:
`plain()` scans the whole text, `TokenSanitizer` the held tail plus each new
chunk. They used to be two implementations and disagreed (for an OSC string
ended by U+009C, `plain()` printed what came before it and the stream also what
came after), so a text and the same text in chunks now print the same by
construction, and a differential test keeps the streaming side honest (below).
Both remove terminal control sequences (7-bit and 8-bit OSC, DCS, SOS, PM, APC
and CSI, and the remaining C0/C1 controls; tab and line feed are kept, and a
carriage return is handled as below) and the *hidden characters*: the server
guard's fold and taint tables (`_INVISIBLE_RANGES` and `_TAINT_RANGES` in
`src/server/utils/output_guard.py`), code point for code point: bidi marks,
embeddings, overrides and isolates, the tag block and variation-selector
supplement, zero-width space and joiners, the word joiner and invisible
operators, fillers, `FE00`-`FE0F`, the BOM, the soft hyphen and every other
Unicode format character, plus lone surrogates, which cannot be encoded. Hidden
characters go first, so a control sequence with one inside it is still removed
whole, and they are removed per code point, so a split never hides one from the
filter. A carriage return becomes a line break (a CRLF split over two chunks is
still one, and a chunk that holds only characters the sanitizer deletes, such as
a NUL, does not make it forget a pending carriage return): on a terminal it
returns the cursor, so print mode, which writes straight to stdout, would let a
later part of a line overwrite an earlier one.

What the scanner reads, left to right, with every sequence ending at its own
terminator. What a recognised sequence covers is dropped (a string control's
payload and a CSI sequence's parameters are never printed) and every character
outside one is shown as the text it is, so a character after a broken, cut-off or
nested sequence is shown. This is **not** "the least that any terminal might
hide": where terminals are not known to agree on where a string control ends, the
scanner takes the reading in which it ends later (the `ESC ]` rule below), and
for `ESC ]`, U+009C, U+009E, BEL, text the other reading hides the text and this
one shows it. What is guaranteed, and checked for every text of the differential
test, is that no ESC, C1 control, hidden character or C0 control other than tab
and line feed is ever printed:

- A **string control** (OSC `ESC ]`, DCS `ESC P`, SOS `ESC X`, PM `ESC ^`, APC
  `ESC _`, or the C1 forms U+009D, U+0090, U+0098, U+009E, U+009F) is dropped
  from its introducer through its terminator. `ESC \` (ST) ends any of them, BEL
  ends an OSC, and U+009C ends every one **except the 7-bit `ESC ]` OSC**.
  Terminals are not known to agree on whether U+009C ends that one (none was
  tried here), so it stays open until its BEL or ST and what follows is hidden
  (before, the stream ended it at U+009C and printed the text after). BEL is data inside a DCS, SOS, PM or APC. The
  payload is opaque: an introducer inside it is data and the first terminator
  ends the string.
- A **CSI sequence** (`ESC [` or U+009B) is dropped whole. One that ESC or a C1
  control cuts off is dropped as far as the cut (the standard terminal state
  machine cancels it there and draws none of its parameters) and the cutting
  sequence is read afresh. One that any
  other character breaks, such as a line feed, loses only its introducer (the ESC
  of `ESC [`, or U+009B), and what follows, the `[` and the parameters included,
  is ordinary text: `a`, `ESC [ 3`, line feed prints `a[3` and the line feed,
  whether or not more text follows (the old whole-text filter dropped the `[3`
  when the line feed was the last character). A stream also gives up on one that
  is still unfinished after 256 characters, and prints it the same way (the hold
  bounds, below).
- A lone ESC (the character after it stays), any other C1 control and the C0
  controls other than tab, line feed and carriage return are dropped alone.
- At the end of the text, or at `flush()`, whatever is still unfinished (an open
  string control, an unfinished CSI sequence **whatever its length**, a trailing
  ESC) is dropped, never printed.

A sequence nested in or cut off by another is not joined with the text around
it: the text after a complete sequence is text. The old whole-text filter removed
whole string controls first, so `ESC`, a string control and `[31m` were
completed into a CSI sequence and dropped, and a string control nested in
another was removed first, which could leave the outer one without its
terminator and hide everything after it (U+009E U+009D U+009C `\` printed
nothing and now prints `\`). A terminal's state machine does not complete a CSI
sequence across a removed string, and nothing specifies nested string controls,
so the scanner prints those characters as the text they are: characters outside any sequence,
never a payload and never a control. This is the one place the whole-text output
got longer (counted below). `strip_controls()` is still the older filter, one
pass per kind of sequence, and keeps that behaviour: it is for text that is sent
on, not shown.

String controls are removed by a scan that is linear in the text, for `plain()`
and for a stream fed a large chunk alike (tens of KB of `ESC ]` took seconds with
the old lazy patterns). The first terminator after a string control's introducer
ends it, so the scan looks for each kind with `str.find` and remembers where it
last found each (or that none follows the place it looked from); it only moves
forward, so a terminator is searched for again only once the scan has passed it.
One that never occurs, or lies far ahead (BEL in a run of strings that `ESC \`
ends), is therefore read for once over the whole text, and not to the end of it by
every string control in the run, which is quadratic. Measured on one machine
(best of three), `plain()` of `ESC ] x BEL` repeated 20,000, 40,000 and 100,000
times (80,000, 160,000 and 400,000 characters) took 0.007, 0.016 and 0.040 s,
against 0.005, 0.009 and 0.022 s for the filter before this change (about 1.7
times the cost, in proportion to the text); the old stream, fed those 100,000 in
one chunk, took 28.8 s and now takes 0.04 s. The other kinds grow in proportion
too: 16 times the repeats took 14 to 18 times as long for `ESC ] x ESC \`, U+009D
with each of its three terminators, DCS, SOS, PM and APC in both forms (10,000 to
160,000 repeats) and for CSI sequences of both forms, and for sequences cut off or
broken, lone ESC characters, nested introducers and a mixture (5,000 to 80,000
repeats), each whole or fed in 64-character chunks. The exception is the held
string described below. A test counts the characters the terminator searches read
for every kind of string control (at most twice the text for each of its
terminators; a search that starts over for every string read 500 times the text of
a 4,000-character one), and another checks that four times the text takes less
than eight times as long (a linear scan takes about four, a quadratic one sixteen).
`TokenSanitizer` holds an unfinished sequence between
chunks, but only up to a bound: a CSI sequence stops at its first character that
cannot belong to one (a terminal aborts it too) or, while unfinished, once it is
longer than 256 characters counting `ESC [` (its introducer is dropped and the
rest is shown as text), and a string control without a terminator is dropped
once the held text is longer than 65,536 characters (64 Ki; introducer
included), so a stream that never closes one cannot swallow the rest of a run
or grow without limit. The bounds count characters (Python code points) of the
held text, not bytes, and a test pins both edges (256 held and 257 released,
65,536 held and 65,537 released, in the 7-bit and the 8-bit form). **The bounds
are the one place where chunking can change what is shown, and only a stream has
them:** below them any split of a text prints what the whole text prints; beyond
them a stream releases what it holds, while the whole text, which has no
terminator to wait for, drops an unfinished sequence of any length (a million
parameter bytes after `ESC [` at the end of a text print nothing).

A stream fed a string control that never closes, a character at a time, searches
the held text for a terminator again on every chunk, up to the bound. That
search is `str.find` per terminator, as in the old stream; a regular expression
over the alternatives was tried first and was about twelve times slower (65,000
one-character chunks of an unterminated `ESC ]`: 0.45 s with `find`, 5.4 s with
the regular expression, on the machine this was measured on). A test holds the
stream to a small multiple of the cost of the `find` calls themselves, timed in
the same process. The search is still repeated from the introducer on every
chunk, so the cost of one such string grows with the square of its length up to
the bound; it has not been made linear.

`libs/ptc-cli/tests/unit_tests/test_superagent_sanitizer_parity.py` is the
differential test. It cuts every text of up to three characters over 22 (ESC,
`[`, `]`, `P`, `X`, `^`, `_`, `\`, BEL, U+0090, U+0098, U+009B to U+009F, CR, LF,
NUL, a zero-width space, a letter and a digit), every text of four characters
over 12 of them, and of five and six over 6 and 5 of them, into one, two and
three chunks in every place (empty chunks included), and 12,000 seeded random
texts of 5 to 24 characters into one-character chunks and three random cuts;
each must print what `plain()` prints. A deliberately naive reader of the rules
above, sharing only the hidden-character table, must agree with `plain()` on all
of those exhaustive texts (up to three, four, five and six characters) and on
12,000 seeded random texts of 5 to 24 characters of its own (another seed, so not
the random texts that are cut), no control or hidden character is ever printed
for any of them, and the choices above are pinned in both directions (an `ESC ]` string and U+009C,
the grammar of a CSI sequence and one cut off or broken, the payload of every
string control under every split, the unfinished tails at the end, the edges of
the two hold bounds, an unfinished CSI sequence of up to a million characters at
the end of a text, the work and the time of searching for terminators in runs of
terminated strings, and the cost of the held string search). Run against the two
filters as they were before this change, it fails: among the texts of four
characters over the 22 it found 700 on which they disagreed, all with `plain()`
hiding at least as much as the stream.
Measured separately, against the old whole-text filter, 200,000 seeded random
texts of 1 to 12 of the 22 characters gave the same output for 98.8 % of them,
less for 0.2 % and more for 1.0 % (a text in which the two differ in both
directions is rare: none in that sample). So `plain()` now prints more than it
did for about one text in a hundred of that kind. The extra text is characters
outside any recognised sequence: the cases in the paragraph on nested sequences
above (`ESC`, a complete string control, `[31m` prints `[31m`) and the line feed
after an unfinished CSI sequence. It is a loosening of the whole-text filter in
those cases, made on purpose and covered by the rules above, not a tightening.
Against the old stream the output was the same for 99.5 %, less for 0.5 % and
more for two texts in 200,000 (an `ESC ]` string with U+009C in its payload and
another string control after it, which the old stream took for a separate string
and dropped).

The paths covered: live tokens (text, reasoning and tool names) in the TUI and
the line shell, and the text, tool names and activity rows print mode writes
(it does not print reasoning); activity rows, error, gap and reset
notices; the report summary and question; the `/jobs` table and detail and the
event record; the output of `!` and `conductor` commands; and in the TUI the
tool name, status, output lines, permission decision and the status bar. A
block is cleaned when it is drawn, so one restored from a saved session file is
clean too. A root test
(`tests/unit/server/utils/test_client_hidden_text_parity.py`) fails if the client
table misses any code point the guard folds or refuses (it checks that
direction only; the two sets were identical when this was written), and a
ptc-cli test asserts that every format (Cf) code point of the running
interpreter is covered.

The cost, stated exactly: ZWJ, ZWNJ and the variation selectors go with the rest,
because prompt_toolkit counts them as zero cells while a shaping terminal draws
a joined glyph (a layout skew) and they are a one-bit hidden-text channel. So
`plain()` turns an emoji ZWJ sequence into its parts and a flag tag sequence
(England, Scotland, Wales) into the base flag alone, drops a presentation
selector (U+26A0 followed by U+FE0F keeps only U+26A0), removes the ZWNJ/ZWJ
from Persian and Indic spelling, and drops the LRM, RLM and ALM marks. That
those code points are removed was checked by running `plain()`; how a given
terminal then draws the result (separate emoji, a text-style symbol, unjoined
script) was not observed. The server record is unchanged and keeps them, and
an assistant block keeps its original text.
`strip_controls()` removes control sequences only and keeps every printable
character; it is used for text that is sent on rather than shown (an
`@`-attached file reaches the model with its joiners and RTL marks), where
dropping one would change what the model reads. A contextual keep rule (one
joiner between two emoji or script characters) would need held state in
`TokenSanitizer` and give up chunk-split independence; revisit it if RTL or
Indic users report the loss.

Not covered: U+2028 and U+2029 are not stripped (they are not in the guard's
tables); `conductor_cli.sanitize_terminal` (`src/conductor_cli/client.py`, the
`conductor` CLI) is a separate, shorter filter (7-bit OSC and CSI and the
C0/C1 controls; the payloads of DCS/SOS/PM/APC and of 8-bit string controls
stay as text, and no hidden character is removed, checked by running it), and
the legacy `ptc-agent` chat renderer (`ptc_cli/streaming`) does
not use `text.py` either (its own filtering was not reviewed here). The `@`
completion menu draws local file names through `plain()`; what accepting one
inserts stays the real name, so the file still resolves.

## Permissions (HITL)

The TUI never runs a local mutating action without confirmation:

| Action id | Trigger | What runs |
|---|---|---|
| `local-merge` | `/merge …` | `conductor merge …` (rehearse and merge a line after the server-run Codex review says MERGE and the tests are green; see `docs/CLI-SUPERAGENT.md`) |
| `local-review` | `/review …` (every subcommand, `status` included) | `conductor review …` (the cross-review queue; a line cannot review itself) |
| `local-shell` | `!command` | the typed shell command in the launch directory |

The first time, a permission row appears between the conversation and the
composer with **Allow once / Session allow / Deny** (`a`/`s`/`d`, or Esc/Ctrl+C
to deny); the decision is recorded in the conversation. "Session"
remembers the action id until the process exits (never persisted, and `/new`
and `/resume` do not clear it), so a batch of merges asks once, and one session
grant for `local-shell` covers every later `!command`. Gated actions are
serialized: a second one waits for the first permission row. If the session is
switched while a row is open the answer is forced to deny.

The Rich line fallback asks the same Allow/Session/Deny question on stdin. For
`/merge` and `!command` it refuses to run when stdin is not a TTY, so those two
cannot be bypassed by `--no-tui` or a pipe. **`/review` is not gated in the
line shell**: running `SuperAgentShell.handle("/review run a --by b")` with
stdin not a TTY ran `conductor review` without asking (checked on this
checkout; no test covers it), while the TUI asks. Treat that as a gap in the
line shell, not as a guarantee. `--auto-approve` is a
`ptc-agent` chat flag and is intentionally not wired into the Super-agent
modes: this UI has no server tool-approval channel, and the server's own
Worker policy still governs remote tools; the gate covers only what this
process executes locally.

## Sessions

Sessions are JSON files under `$CONDUCTOR_HOME/superagent-sessions`
(default `~/.conductor/superagent-sessions`, override with
`CONDUCTOR_SUPERAGENT_SESSIONS`), written atomically with mode 0600 in a 0700
directory. A record holds the transcript blocks (stored as the server sent
them, cleaned only when drawn), Home workspace, chosen daemon,
router model, last Ticket id, and timestamps. `cond --continue` loads the most
recent; `/resume` lists the newest 10 (id, title, last Ticket; the title is
always the fixed `super-agent`) and `/resume <id>` switches (the current
session is saved first). A corrupt file, a wrong-shaped field or a block this
version cannot read is skipped, reading through a symlinked session directory
or file is refused, writes use a fresh `O_EXCL` 0600 temp file before the
atomic rename, a path-traversing id is refused, and the store keeps at most 50
sessions. Every TUI launch saves a session, even one with no prompt, so empty
launches count against the 50. `--continue --no-tui` still reads a session for
the daemon/last-Ticket notice but does not write a transcript. The resume
listing prints full session ids (16 hex characters) and `/resume` also accepts
an unambiguous prefix (an exact id wins over a longer shared prefix). A
session switch (`/new`, `/resume`) bumps a session epoch and cancels/joins
in-flight submissions and the current watch, so a Ticket created by a stale
submission cannot land in the new transcript.

## `@file` attachments

Tab completes from the launch directory (names starting with `.` and
`node_modules`, `__pycache__`, `.venv`, `venv`, `dist`, `build` are pruned; the
walk stops after 5 000 files and the menu shows at most 60 candidates). On
send, `@path` (relative to the launch directory, or absolute) is read locally
and appended to the prompt as `Attached file: <path>` plus a fenced section per
file, so the server sees the content and path; control sequences are stripped
from both but joiners and RTL marks are kept. A trailing `.,:;)]}` is not part of the path, and an `@`
glued to a word (an e-mail address) is not a mention. Limits: UTF-8 text only,
no NUL bytes, ≤ 200 000 bytes per file, ≤ 8 attachments; each refusal is shown
as an error block and does not drop the typed line (the prompt goes without
that file). The conversation shows the typed line with an `attached: <path>`
row per file, not the expanded prompt.

## Server field

`GET /api/v1/autonomy/overview` gains `router_model: string` (the configured
`autonomy_router_model`, or `""`; the committed `config.yaml` sets it to
`deepseek-flash-account`, DeepSeek-V4.1-Flash through the account sign-in). It
is read-only configuration, not a secret. The status bar shows `unknown` for a
deployment without the field, or with an empty one, until a value is known (a
restored session brings back its last one).

## Files

- `libs/ptc-cli/ptc_cli/superagent/text.py` — terminal-control- and
  hidden-character-safe text shared with the line shell (moved out of
  `shell.py`, whose tests still import the old private names); see
  [Terminal safety](#terminal-safety).
- `tui_events.py` — SSE/polling watch; `tui_blocks.py` — conversation model,
  reducer and renderers; `tui_sessions.py` — session store;
  `tui_permissions.py` — Allow/Session/Deny gate; `tui_local.py` — local
  subprocesses; `tui_completion.py` — `/` and `@` completion + attachments;
  `tui_rich.py` — the welcome, `/help` and `/model` panels captured as plain
  text; `tui.py` — controller, print mode and the prompt_toolkit application.
- `runner.py` — mode selection entry point; `shell.py` keeps a
  backwards-compatible `run_super_agent` alias.
- `libs/ptc-cli/ptc_cli/main.py` — `-p/--print`, `--continue`, `--no-tui` for
  `--super-agent` (`--exec` and `--no-splash` were already there).
- `scripts/cond` — usage documents the TUI modes; it runs
  `ptc-agent --server … --user-id local-dev-user --super-agent "$@"`, so the
  flags pass through unchanged.
- `src/server/models/autonomy.py`, `src/server/app/autonomy.py` — overview
  `router_model`.
- Tests, all under `libs/ptc-cli/tests/unit_tests/`:
  `test_superagent_tui_units.py` (events, blocks, sessions, completion,
  permissions, mode choice), `test_superagent_tui_app.py` (the controller, the
  prompt_toolkit app and print mode), `test_superagent_hidden_text.py` and
  `test_superagent_text_strings.py` (hidden characters and string controls),
  `test_superagent_carriage_return.py` (a carriage return becomes a line
  break in every display path, a CRLF split over chunks, print mode writes no
  carriage return), `test_superagent_sanitizer_parity.py` (a text and every
  split of it print the same, against a naive reader of the rules, and the
  choices that were forced), and the line-shell and flag tests
  `test_superagent_shell.py` and `test_main_args.py`.

## Verification

Counted on the mainline checkout at `d12301872` (2026-11-05), from the
repository root with a virtualenv that has the `cli` extra (`uv sync --extra
cli`); `PYTHONPATH` puts `ptc_cli` and `src` on the path. The two directory
totals and the parity file's count were re-counted on `fix/sanitizer-stream-parity`
after the sanitizer-parity tests were added and extended (the totals were 528 and
551 before them):

```sh
PYTHONPATH=libs/ptc-cli:. python -m pytest -p no:cacheprovider -q \
  libs/ptc-cli/tests/unit_tests/test_superagent_tui_units.py \
  libs/ptc-cli/tests/unit_tests/test_superagent_tui_app.py \
  libs/ptc-cli/tests/unit_tests/test_superagent_shell.py \
  libs/ptc-cli/tests/unit_tests/test_main_args.py       # 233 passed

PYTHONPATH=libs/ptc-cli:. python -m pytest -p no:cacheprovider -q \
  libs/ptc-cli/tests/unit_tests                          # 603 passed

PYTHONPATH=libs/ptc-cli:. python -m pytest -p no:cacheprovider -q \
  libs/ptc-cli/tests                                     # 626 passed, 2 skipped

PYTHONPATH=libs/ptc-cli:. python -m pytest -p no:cacheprovider -q \
  tests/unit/server/test_autonomy_api.py \
  tests/unit/server/utils/test_client_hidden_text_parity.py   # 43 passed
```

Per file: `test_superagent_tui_units.py` 95, `test_superagent_tui_app.py` 78,
`test_superagent_hidden_text.py` 31, `test_superagent_text_strings.py` 19,
`test_superagent_carriage_return.py` 9, `test_superagent_sanitizer_parity.py` 75,
`test_superagent_shell.py` 56, `test_main_args.py` 4 (each run alone; the first
command above leaves out the hidden-text, text-strings, carriage-return and
sanitizer-parity files). The two skipped tests are
the live workspace tests in
`libs/ptc-cli/tests/integration_tests/test_session_persistence_real.py`, which
run only when `PTC_CLI_TEST_SERVER_URL` is set. Of the last command, 42 tests
are in `test_autonomy_api.py` (it covers the overview, including
`router_model`) and 1 is the hidden-character parity test. These counts move
with every added test; re-run to get the current ones.

The ptc-cli tests above use fake clients and fake streams (`FakeClient`,
`FakeStreamClient`, `httpx.MockTransport`), so they need no backend, provider,
database or network. The application tests run the real prompt_toolkit app
through `create_pipe_input` and a `DummyOutput`/`PlainTextOutput`. The keys they
send are Enter, Esc then Enter, the kitty Shift+Enter CSI sequence (`13;2u`),
Up (history recall), the `d` permission key, Esc (denial of a pending
permission), Ctrl+C (single and double) and PgUp/PgDn; they also check line
rendering, page scrolling and session-switch watch cancellation. No test in
`test_superagent_tui_app.py` sends Down, the xterm `27;2;13~` Shift+Enter form,
`a` or `s` as keys (allow once and session allow are tested through the
controller instead), Ctrl+T, Ctrl+O, Ctrl+D or Tab, so those bindings are
unpinned. Print mode is checked against streamed tokens split across frames (joined on one
line), OSC/BEL-carrying text, ticket ids and server errors, resets and gaps,
and emits no terminal controls. Standard unified diffs without a `diff --git`
header are parsed too.

Also run directly while writing this page (not part of the suite): the
sanitizer on CR/CRLF splits, hidden characters and 7-bit and 8-bit string
controls (the two hold edges are in the suite now); the
watch's reconnect delays and its polling fallback against fake streams,
including a clean close after a frame and an `error` frame as the first frame
(both reconnect five times, then poll); the status bar after a daemon listing
turns empty (`no daemon (online)`); a session save through the controller
(file mode 0600, directory 0700, 16-hex id, one file for a launch without a
prompt); and the line shell's gate with stdin not a TTY (the `/review` gap
under [Permissions](#permissions-hitl)).

Limits: the backend's Ticket activity projection emits tool names only
(arguments/results are stripped server-side on purpose), and the client does not
read tool arguments or diffs even if a payload carries them, so the TUI shows
tool names, never parameters or a tool's diff; a diff inside the assistant's own
text is colored when it has a file header and an `@@` hunk header. Not verified: the interactive TUI against a live backend, a
real daemon or a real terminal (no screenshot or live call was made for this
page), so the layout above is an illustration from the code, and how Shift+Enter,
Esc timing and colors behave in a given terminal is untested. The token
estimate is approximate, narrow-terminal switching happens at launch only, and
Shift+Enter depends on the terminal reporting the key (documented fallbacks
otherwise). Rich rendering differences between terminals are not asserted; the
tests assert controller state and frame text.

## Rollback

Written for the original change and not rehearsed against this tree. The ten
modules it added are `text`, `tui_events`, `tui_blocks`, `tui_sessions`,
`tui_permissions`, `tui_local`, `tui_completion`, `tui_rich`, `tui` and `runner`
(all under `libs/ptc-cli/ptc_cli/superagent/`), plus the TUI test files, the
three `main.py` flags, the `router_model` overview field and the `scripts/cond`
usage text. Later work depends on them (the daemon output stream's client side,
the reset/epoch fences, the hidden-character sanitizer, names-only tool
blocks, and `shell.py` importing `text.py`, `tui_events.py`, `tui_local.py` and
`tui_permissions.py`), so a revert has to take those commits out as well.
Sessions stay on disk but are unused; delete
`$CONDUCTOR_HOME/superagent-sessions` (default
`~/.conductor/superagent-sessions`) to remove them.
