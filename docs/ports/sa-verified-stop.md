# sa-verified-stop — review fix summary

Status: complete on branch `sa-verified-stop`. This is the review follow-up to
the §4.1 verified-stop work in the internal document `verified-stop` (not published);
the full design, state machine, and original test log live there.

## Latest review fix (detached descendants)

The latest rereview (`logs/codexrev--sa-verified-stop--rereview-sa-verified-stop.log`)
returned one open finding; the two earlier P2s were confirmed fixed.

1. **`gone()` missed detached descendants** (`src/llms/native_cli/process.py`,
   reported at `process.py:81`). The tracker only checked each leader and its
   original process group. A CLI child started with `start_new_session=True`
   leaves that group, survives the cancellation `killpg`, and `gone()` still
   returned `True` — a false verified stop. Reproduced offline before fixing:
   detached child alive, `tracker.gone() == True`, evidence listing only the
   leader.

   Fix — track **and** contain at kill time, while the leader is alive (after
   it exits, detached children are reparented away and undiscoverable):

   - `_descendants()` takes one `ps -e -o pid=,ppid=,pgid=` snapshot and walks
     the transitive children of a pid; a `ps` failure degrades to no
     descendants instead of an error on the cancellation path.
   - `ProcessTracker.capture_descendants()` records those rows (deduped,
     never a `pgid` of 0, which would mean "our own group" in `killpg`).
   - `_kill_tree(process, tracker)` snapshots first, kills the leader's
     session as before, then kills each escaped descendant's group; all four
     call sites in `run_process`/`stream_process` pass their tracker. The
     tracker keeps the descendant rows, so `gone()` and `evidence()` verify
     their death, not just the leader's — a survivor now yields
     `process_gone=false` → `unverified` instead of a false verified stop.

## Prior review fixes

The earlier round fixed three findings. No new legacy source was ported; the
changes extend Conductor's existing autonomy Ticket/daemon path.

1. **Missing runner metadata was treated as termination**
   (`src/server/database/autonomy/stops.py`). `request_stop` now separates a
   never-claimed Ticket from one whose lease metadata was cleared by
   `recover_expired_daemon_leases`. A daemon Ticket with launch history
   (`attempts`, `started_at`, claim/thread/run ids) but no live lease becomes
   `daemon`/`unverified` with immediate escalation and `stop_unverified`
   attention; a backend launch with history but no persisted thread id becomes
   `backend`/`not_applicable`. Only a Ticket that never claimed a runner is
   `not_started`/`verified`, recorded with `never_started` evidence.
2. **A pending backend launch could run after a verified stop**
   (`src/server/services/autonomy/worker.py`). `_launch_queued` fences its
   `queued → running` transition with `expected_status="queued"` and aborts if
   a stop wins during thread creation. `_start_run` records the run only while
   the Ticket is `running`, and cancels the run it just started when a stop
   landed during dispatch. The dispatch-failure path uses the expected status
   so it cannot overwrite `cancelled` with `failed`.
3. **The stop acknowledgement request ignored its budget**
   (`scripts/conductor_local_daemon.py`). Each `api.stop_ack` call now receives
   the remaining acknowledgement budget as its request timeout, so a hung HTTP
   call cannot consume the default 30s inside a 10s budget.

## Tests

Regression tests for the latest finding —
`tests/unit/llms/test_native_cli_process_tracker.py` (+3):

- `test_kill_tree_contains_and_tracks_a_detached_descendant` — the setsid
  child dies with the tree kill and appears in the evidence rows.
- `test_a_live_detached_descendant_blocks_gone_until_it_dies` — with only the
  leader group killed, `gone()` stays `False` while the detached child runs
  and flips `True` once it dies (the reported false-verified-stop case).
- `test_cancelling_run_process_kills_and_tracks_the_detached_child` — the
  end-to-end cancellation path through `run_process` passes its tracker into
  the tree kill.

Regression tests from the earlier round:

- `tests/unit/server/database/test_autonomy_stops.py` — 3 (lease-recovery
  unverified, backend launch without a thread, never-claimed verified)
- `tests/unit/server/services/autonomy/test_worker_stop_race.py` — 3 (stop
  during thread creation, stop during dispatch, dispatch failure after stop)
- `tests/unit/scripts/test_conductor_local_daemon.py` — 1 (per-request stop-ack
  timeout shrinks with the remaining budget)

Focused run with the repo `.venv` fallback:

```bash
../../Documents/deepseek-harness/default-workspace/AlphaCN/.venv/bin/python -m pytest \
  tests/unit/server/services/autonomy \
  tests/unit/server/database/test_autonomy_daemons.py \
  tests/unit/server/database/test_autonomy_ticket_inputs.py \
  tests/unit/server/database/test_autonomy_stops.py \
  tests/unit/server/database/test_autonomy_effects.py \
  tests/unit/server/database/test_autonomy_deliberation_rows.py \
  tests/unit/server/database/test_autonomy_deliberation_guards.py \
  tests/unit/server/database/test_autonomy_watchdog_query.py \
  tests/unit/server/test_autonomy_api.py \
  tests/unit/server/test_autonomy_daemon_followup.py \
  tests/unit/server/test_autonomy_stop_api.py \
  tests/unit/server/test_import_layering.py \
  tests/unit/scripts/test_conductor_local_daemon.py \
  tests/unit/scripts/test_refactor_status.py \
  tests/unit/llms/test_native_cli.py \
  tests/unit/llms/test_native_cli_process_tracker.py \
  tests/unit/migrations tests/unit/config/test_infrastructure_config.py -q
```

Result after the detached-descendant fix: **843 passed, 1 skipped** (was 840
passed, 1 skipped). Also ran the whole `tests/unit/llms/` suite: **1338
passed, 20 skipped**. `ruff check` passed on both changed files;
`ruff format --check` passed on the test file — `src/llms/native_cli/process.py`
retains only the formatting drift it already had at `HEAD`.

## Limits

Verification is unit-level with fake dispatchers, scripted DB cursors, and a
fake daemon tracker. The detached-descendant fix was reproduced and re-verified
offline with real processes on this machine (both the contain and the
track-only paths), but no live native CLI daemon was stopped end-to-end, no
real daemon timeout was raced, and no production database migration was
applied. A descendant that creates a *new* detached child in the window
between the `ps` snapshot and the kill would still escape; verification stays
honest in that window (`gone()` sees the recorded survivor) but does not kill
it.
