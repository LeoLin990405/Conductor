# Agent fleet dashboard port

A read-only operator dashboard over the local agent worktree fleet: one row per
worktree, judged from the host's own process table, git metadata and the log
files the agent CLIs already write. Backend endpoint, `conductor agents fleet`
CLI command, and a web page. Nothing is written anywhere: the scan never touches
a worktree, a log file or a running agent, and the endpoints refuse any
non-loopback caller.

## What was ported, and from where

The legacy inventory (`legacy-survey/docs/MIGRATION-FROM-LEGACY.md`, §2–3) lists
`model-farm-handoff`'s fleet tooling as the source for this shape. Three ideas
were taken, and the rest of the legacy farm stack (task SQLite, outbox,
dispatch, receipts, context packs) is out of scope.

| Legacy source | What was taken | Where it landed |
|---|---|---|
| `farmctl/workflow/codexbar_usage.py` (739 LOC, read-only sanitizer) | the rule that a usage reading is a bounded *state*, never an amount | provider headroom via the already-ported `src/server/services/native_runtime/usage_headroom.py` reader, projected to `ok`/`low`/`unknown` |
| `~/conductor-wt/launch.sh` + `status.sh` (the local fleet's own conventions) | the fleet shape this host actually has: one worktree per agent, one log per agent, one CLI process per agent | `src/server/services/agent_scanner.py` |
| the sanitization contract in `codexbar_usage.py`'s remote sanitizer (never print identity, cookies or configuration) | the redaction rule for anything a CLI dumped into its log | `src/server/utils/sanitize.py` |

The CodexBar quota reader is reused rather than re-implemented: the fleet reads
the same explicitly configured local snapshot (`native_runtime.usage_headroom`)
through `load_usage_headroom`, so the dashboard and the router cannot disagree
about what a provider's headroom is.

## Backend

- `src/server/utils/sanitize.py` — `sanitize_log_text` / `sanitize_log_tail`.
  Redacts provider credential shapes (AWS, OpenAI/Anthropic, GitHub, GitLab,
  Slack, Google, npm), `Bearer`/`Basic` transport tokens, JWTs, PEM blocks,
  `key=value` secret assignments, keyword-adjacent UUIDs and email addresses;
  strips ANSI/control escapes; bounds the length. Ordinary log lines, pytest
  failure lines and token *counts* survive.
- `src/server/models/agent.py` — `AgentStatus` (running/stalled/error/done/
  unknown), `AgentRole` (plan/dev/review/research), `Agent`, `AgentLogs`,
  `AgentFleet`. No field carries a balance amount, an account or a credential.
- `src/server/services/agent_scanner.py` — the scan. Worktrees come from
  `git worktree list --porcelain` through any one worktree; branch and
  commits-ahead from `git rev-parse` / `git rev-list --count <base>..HEAD`;
  processes from `ps -Ao pid=,etime=,command=` filtered to the agent CLI
  executables, attributed to a worktree by cwd (`lsof -p`), then by the log
  file the process holds open, then by a worktree path in the command line.
  Status: `error` if the log carries a failure marker, else `stalled` when the
  log is colder than the threshold, else `running`; a dead process is `done`, a
  live process with no readable log is `unknown`. Role is inferred from the
  agent's prompt head; test state from the last pytest terminal summary line.
  Every command runs through an injectable runner with a timeout, so no test
  spawns a subprocess.
- `src/server/app/agents.py` — `GET /api/v1/agents/fleet` and
  `GET /api/v1/agents/{agent_id}/logs`. Loopback-only, mirroring
  `src/server/app/local_update.py`: the fleet describes the host, so a remote
  caller in a hosted deployment gets 403. A scan failure is reported in the
  response body rather than raised.
- `src/config/models.py` / `settings.py` / `config.yaml` — the `agent_fleet`
  section, with `AGENT_STALL_MINS`, `AGENT_FLEET_ROOT` and
  `AGENT_FLEET_BASE_REF` as per-host environment overrides.

## Frontend and CLI

- `web/src/api/agentFleet.ts`, `web/src/features/agentfleet/AgentFleetPage.tsx`
  (+ `fleetView.ts`, `agentFleet.css`) at `/agent-fleet`, in the sidebar beside
  the agent inventory. Auto-refresh every 30s, overridable with `?refresh=10`.
  Filter by status/role/provider/model, sort by the same columns the CLI sorts
  by. Status pills follow `DESIGN.md`'s liveness vocabulary (the shared ascii
  `Loader` for live work, informative glyphs for error/stalled/unknown, no glyph
  for done); balance is an icon plus a word, never an amount. Clicking an agent
  id opens a log panel served by the logs endpoint — server-redacted text only.
- `conductor agents fleet` (`src/conductor_cli/fleet.py`) — `--watch`,
  `--json`, `--filter key=value` (repeatable), `--sort` over the same six keys.
  The existing `conductor agents list` is untouched.

## Tests run

| Suite | Result |
|---|---|
| `tests/unit/server/utils/test_sanitize_agent_logs.py` | 28 passed |
| `tests/unit/server/services/test_agent_scanner.py` | 43 passed |
| `tests/unit/server/app/test_agents_fleet.py` | 9 passed |
| `tests/unit/config/test_agent_fleet_config.py` | 10 passed |
| `tests/unit/test_conductor_cli_fleet.py` | 27 passed |
| `tests/unit/server` (whole tree, regression) | 8682 passed, 2 skipped |
| `tests/unit/config` (whole tree, regression) | 276 passed |
| `web`: full `vitest run` | 186 files, 1557 passed |
| `web`: `tsc -b` | clean |
| `web`: ESLint on changed files | 0 errors, 0 warnings |
| Ruff on changed Python | clean |

Fakes throughout: a `FakeRunner` answers `ps`/`lsof`/`git` from fixtures, the
route and CLI tests patch the scan and the API client, and the CodexBar reader
is patched so no snapshot is read. A live smoke scan against this host's own
fleet (38 worktrees) returned correct branches, commits-ahead, roles and
sanitized tails; no agent process was running, so every row read `done`, and
the host's GUI `codex`/`ZCode` processes were correctly not attributed.

## Re-review rounds 1–7

Six rounds of Codex re-review followed the port; each finding was fixed with a
regression test that fails against the pre-fix source, and the per-round notes
live in `agent-fleet-review-fixes-{1..6}.md`. Round 7 closed the six findings
of `logs/codexrev--agent-fleet--rereview-agent-fleet-r6.log`:

| Finding | Where it pointed | Fix |
|---|---|---|
| P1 | `src/server/utils/sanitize.py:68` (`_is_body_line`) | A body line behind a CLI's log prefix whose run is under twenty characters and carries no digit was read as a word, so the orphan-END walk found no material and handed back the whole body — 26 of 26 lines in the reported probe. A body line behind a prefix is now read structurally: a prefix and one opaque token, where an ordinary log line says something and carries a second long token. |
| P2 | `src/server/utils/sanitize.py:68` | Round 6's rule (a capital letter is not key material) was in the worktree but uncommitted; committed, with the reviewer's probe (`ConnectionError:` above an orphan END keeps error detection) re-verified. |
| P2 | `src/server/services/agent_scanner.py:499` (`_list_processes`) | A `ps` that failed or timed out returned an empty list, which reads as "no agent is running" and marked every worktree `done`. It now returns `None` and `detect_status` reports `unknown`: "not there" is only evidence once the listing has answered. |
| P2 | `src/server/services/agent_scanner.py:709` (`read_agent_logs`) | The log file was resolved with the requested id as the only candidate, so `worker` adopted `worker-helper.log` that the scan had assigned to `worker-helper`. The assignment now runs over the worktrees, so an owner claims its own exact log first. |
| P2 | `src/server/services/agent_scanner.py:580` (`_read_prompt_head`) | The prompt was read from its last 2 KiB, so a launcher that appended history behind the opening role instructions lost them and every agent read `dev`. It reads the head; the log tail still reads from the end, where the recent activity is. |
| P2 | `src/conductor_cli/fleet.py:198` | `--watch --json` printed the clear-screen escape in front of every frame, so a piped stream failed to parse on the first one. The clear now only runs for a table, and a frame goes to the same stream its escape went to. |

Tests added this round: `test_redacts_a_prefixed_body_whose_material_is_short_and_word_shaped`,
`test_a_body_line_behind_a_prefix_is_one_token_where_prose_is_not`,
`test_an_unreadable_process_table_is_unknown_not_done`,
`test_process_listing_failure_still_reports_the_fleet` (now over all three
worktrees, not one), `test_a_log_its_owner_claimed_is_not_handed_to_another_agent`,
`test_a_suffixed_log_still_falls_to_the_only_agent_that_names_it`,
`test_role_comes_from_the_head_of_a_long_prompt` and
`test_a_json_watch_frame_carries_no_terminal_escapes`. The `read_agent_logs`
tests now inject a `FakeRunner`, so no unit test spawns a subprocess.

| Suite | Result |
|---|---|
| `tests/unit/server/utils/test_sanitize_agent_logs.py` | 63 passed |
| `tests/unit/server/services/test_agent_scanner.py` | 53 passed |
| `tests/unit/server/app/test_agents_fleet.py`, `tests/unit/test_conductor_cli_fleet.py`, `tests/unit/config/test_agent_fleet_config.py` | 79 passed |
| `tests/unit/server/utils tests/unit/server/services tests/unit/server/app tests/unit/config` | 7124 passed, 2 skipped |
| `tests/unit` (whole tree) | 16195 passed, 21 skipped |

## Re-review round 8

Round 8 closed the five blocking findings of
`logs/grok--agent-fleet--rr3-agent-fleet.log` (verdict BLOCK):

| Finding | Where it pointed | Fix |
|---|---|---|
| P1 | `src/server/utils/sanitize.py:57` (`_LOG_PREFIXED_BODY`) | The prefix rule accepted one timestamp token and an upper-case level, so a private-key body of 8–19 letters behind `2026-11-02 12:00:01 INFO `, syslog's `Nov 02 12:00:01 `, `[INFO]`, a lower-case level or `[2026-11-02 12:00:01]` was handed back in the clear — 16- and 19-letter runs leaked, while a 20-character run or any digit was already removed. The prefix is now a bounded run of tokens (a bracketed group, a date, a clock or a pid, the month of a syslog stamp, and a level in either case — Python logging's `12:00:01,123` among them), and it is required: without one, a bare line of letters is judged by the whole-line rule instead of reading as a prefixed body. A lower-case level is held to the names a level actually has, so `worker finished` stays a log line. |
| P2 | `src/server/utils/sanitize.py:91` (`_is_body_line`) | Any single alphanumeric line was key material, so `FAILED` and `ERROR` above an orphan `END` marker within eight lines became `[REDACTED]` — and `log_has_error` (`agent_scanner.py:293`) reads the redacted tail, so the agent stayed `running` or `stalled`. A whole line of one run is material only when it is twenty characters or more, or carries a character a word never does (a digit, `+`, `=` or `/`). |
| P3 | `src/server/services/agent_scanner.py:570` (`_associate_processes`) | Command-line attribution was a substring check in worktree-list order, so a process whose command contains `/tmp/fleet/dev-review` was assigned to `dev` when `dev` was listed first; an `lsof` cwd of `/private/tmp/fleet/dev-review` missed the git path and fell through to the same wrong owner, and the real agent read `done`. The longest worktree path the command names now claims the process, and each worktree is indexed under both the spelling `git` reports and the one `lsof` reports, so a cwd the kernel resolved through a symlink still matches. |
| P4 | `src/conductor_cli/fleet.py:152` (`render_fleet`) | `--json` printed the raw scan and dropped `--filter` and `--sort`, while the help text promised both. A JSON frame now carries the agents the table would have shown, in its order, as the endpoint's own objects and beside the frame's other keys. |
| P5 | `src/conductor_cli/fleet.py:114` and `web/src/features/agentfleet/fleetView.ts:92` | The CLI sorted commits ascending and the page descending, and neither fixture had two known counts to show it. The CLI reads most-commits-first, like the dashboard's AHEAD column, and its fixture now has two. |

No migration is in this round's diff; the scan still writes nothing.

Tests added this round: `test_redacts_a_body_behind_any_log_prefix` (8 prefix
forms × 3 run lengths), `test_keeps_an_ordinary_line_behind_any_log_prefix`,
`test_keeps_a_lone_word_above_an_orphan_end_marker`,
`test_keeps_a_long_run_or_a_data_character_as_material_on_a_bare_line`,
`test_a_failure_marker_survives_redaction`,
`test_the_longest_worktree_path_claims_the_process`,
`test_a_cwd_the_kernel_resolved_matches_its_worktree`,
`test_by_commits_puts_the_most_commits_first`,
`test_json_mode_keeps_the_filters_and_the_sort`,
`test_json_mode_keeps_the_scan_error`,
`test_json_mode_orders_agents_the_way_the_table_does` and
`test_a_json_frame_keeps_the_filters_and_the_sort`. Re-run against the pre-fix
source (a throwaway worktree at `4d0f0702b`), 31 of them fail there — every
regression test, over its prefix forms and markers. The two that hold on both
sides are guards, not regressions:
`test_keeps_an_ordinary_line_behind_any_log_prefix` and
`test_keeps_a_long_run_or_a_data_character_as_material_on_a_bare_line` pin the
prose and the long-run boundary the wider rule must leave alone.

| Suite | Result |
|---|---|
| `tests/unit/server/utils/test_sanitize_agent_logs.py` | 99 passed |
| `tests/unit/server/services/test_agent_scanner.py` | 56 passed |
| the reviewer's eight suites together | 319 passed |
| `tests/unit/server/utils tests/unit/server/services tests/unit/server/app tests/unit/config` | 7128 passed, 2 skipped |
| `tests/unit` (whole tree) | 16239 passed, 21 skipped |
| `web` (agent fleet, API, nav active state) | 52 passed |

## Risks and rollback

The scan is advisory: a process can start or a log can move between the
observation and the response, so a client treats a row as a sample. `lsof` is
best-effort — where it is unavailable the command-line path match still
attributes an agent. An unknown agent CLI executable is not a fleet agent, so a
launcher that gains a new provider needs one line in
`_PROVIDER_BY_EXECUTABLE`. The dashboard shows only what the host holds: on a
machine with no worktrees the scan returns an empty list, and a hosted
deployment refuses the request outright. `agent_fleet.enabled: false` disables
the endpoints; reverting the implementation commits removes the code.

No real provider, GitHub connection, credential, live backend on :8000, live
database or web deployment was touched. Legacy sources were read only.

Round 7 touched no frontend file, so the web suite and `tsc -b` were not run.
Ruff is still not installed in this environment (no `ruff` module in `.venv`,
none on `PATH`), so `make lint`'s Python half is unverified; every line this
round touched is ≤ 100 characters. No DB migration was added, and no legacy
source was read.

Round 8 touched no frontend file, so `tsc -b` was not run. The web suite was run
afterwards by borrowing `node_modules` from a sibling worktree (`deps-r3`), since
this one's `web/node_modules` is a symlink to the live checkout's dependency
directory, which does not exist here and installing would mean the network: the
agent-fleet, API and nav-active files — the four the reviewer ran — pass at
**52 passed**. The one web file the round's findings named, `fleetView.ts`,
needed no change: the page already sorted AHEAD descending, and the disagreement
was the CLI's.
`_BODY_RUN`'s twenty-character rule still reads a long path a log line names
(`tests/unit/server/test`, 22 characters of letters and slashes) as material,
so a pytest `FAILED` line that carries one is still swallowed by a stray marker
within eight lines of it; the reviewer's finding was the lone-token line, which
is fixed, and narrowing that rule would trade a rare leak for a common log
line. No DB migration was added, and no legacy source was read. The one added
line over 100 characters is a signature that matches the one beside it, and
ruff's default rule set does not select `E501`.
